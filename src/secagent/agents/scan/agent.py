"""UC4 orchestration: LLM rule-based memory/stability scan of C/C++ code."""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from ...affordances import queries
from ...affordances.io_map import component_for
from ...affordances.languages import detect_language, rel_posix, walk_files
from ...affordances.priority import path_rank
from ...audit import get_audit_logger
from ...config import LLMConfig, Settings
from ...llm.client import LLMClient
from ...output import Coverage, Provenance, UnitState, coverage_to_dict
from ...sanitize import harden_system_prompt, wrap_untrusted
from ...security import enforce_fips_policy
from .models import ScanFinding
from .report import render_markdown, summarize
from .rules import (
    SEVERITY_RANK,
    RuleSet,
    load_rules,
    meets_threshold,
    rule_groups,
    rules_prompt,
)

log = logging.getLogger(__name__)

_OUTPUT_INSTRUCTION = (
    "Report rule violations as a JSON array and NOTHING else. Each item: "
    '{"rule": "<rule id>", "line": <int>, "severity": "<severity>", '
    '"message": "<one concise sentence>"}. Use only the listed rule ids. If there are '
    "no violations, return []."
)


def scan_repo(
    repo: str | Path,
    settings: Settings,
    *,
    out_dir: str | Path | None = None,
    paths: list[str] | None = None,
    llm: LLMClient | None = None,
) -> dict[str, Any]:
    """Scan C/C++ files against the configured rule set using the local model."""
    repo = Path(repo).resolve()
    enforce_fips_policy(settings.fips.require_fips, settings.fips.allow_non_fips)
    ruleset = load_rules(settings.scan.rules_profile)

    out = Path(out_dir).resolve() if out_dir else (repo / "secagent-scan")
    out.mkdir(parents=True, exist_ok=True)

    targets, eligible = _select_files(repo, settings, paths, set(ruleset.languages))
    eligible_total = len(eligible)
    skipped_by_cap = eligible[len(targets):]

    owns_llm = False
    if llm is None:
        from ...netpolicy import enforce

        enforce(settings)
        llm = LLMClient(settings.llm)
        owns_llm = True

    findings: list[ScanFinding] = []
    runs_done = max(1, settings.scan.runs)
    failures: dict[str, str] = {}   # file -> why the analysis did not run
    partial: dict[str, str] = {}    # file -> how much of it the model never saw
    # Read-time truncation notes, kept separate from `partial` until `failures` is known.
    # `failed` dominates `partial`: a file whose rule groups ALL failed was never
    # analysed at all, so a truncation note about it is moot for counting purposes (but
    # still worth keeping in the failure reason — a reader still wants to know the file
    # was clipped). Composing `partial` only after `failures` is final is what makes "one
    # file counted as both partial and failed" structurally impossible rather than a case
    # to remember to handle.
    truncated: dict[str, str] = {}
    scanned = 0
    # Populated inside the indexed-store region below, before it closes — `Provenance`
    # is built once the store is gone, same as the coverage block.
    used_llm_summary = False
    summary_model: str | None = None
    try:
        store = queries.ensure_indexed(repo, settings)
        try:
            total = len(targets)
            log.info("scan: %d file(s) against rule set '%s' (model %s)",
                     total, ruleset.name, settings.llm.model)
            # Scanning the whole project is the default because a partial scan of a
            # memory-safety tool is the dangerous kind of incomplete. But one model call
            # per file runs minutes each locally, so say what that costs BEFORE spending
            # it rather than leaving someone to wonder if it hung. Quiet on small repos:
            # a notice that always fires is one nobody reads.
            if settings.scan.max_files <= 0 and total > _COST_NOTICE_FILES:
                log.warning(
                    "scan: no file cap set, so all %d file(s) will be analysed. A local "
                    "model runs minutes per file — expect hours. Pass --max-files N (or "
                    "set SECAGENT_SCAN__MAX_FILES) for a bounded pass.", total)
            groups = rule_groups(ruleset, settings.scan.effective_granularity())
            prompts = {cat: _system_prompt(sub) for cat, sub in groups}

            def _one(item: tuple[int, str, str, int]) -> tuple[int, str, str,
                                                               list[ScanFinding], str]:
                idx, rel, cat, _run = item
                capped = _read_capped(repo / rel, settings.scan.max_file_bytes)
                if capped is None:
                    # A file selected for scanning that could not be read is a FAILURE,
                    # not an absence. It used to vanish without trace; then it was
                    # counted, but against the file cap — so an unreadable file made an
                    # uncapped run report files as "skipped by scan.max_files=0", naming
                    # a setting that had excluded nothing. A wrong reason is worse than
                    # no reason: it sends you to fix something that is not broken.
                    return idx, rel, cat, [], ("could not be read (not valid UTF-8, or "
                                               "unreadable) — it was never analysed")
                text, full_size = capped
                if len(text) < full_size:
                    # Recorded here, not written into `partial` directly: whether this
                    # file ends up `partial` or `failed` is only known after every rule
                    # group for it has run (see the composition below), and a file whose
                    # groups all fail must not appear in `partial` at all.
                    truncated[rel] = (
                        f"analysed the first {len(text)} of {full_size} chars "
                        f"(scan.max_file_bytes={settings.scan.max_file_bytes}); "
                        f"the rest was never read")
                # One line per file BEFORE the call: an LLM scan runs minutes per file on
                # a local model, and with no output at all a user cannot tell a slow run
                # from a hung one. Two devs independently gave up waiting on this.
                log.info("scan: [%d/%d] %s%s …", idx, total, rel,
                         f" [{cat}]" if cat else "")
                header = (_find_header(repo, rel, settings.scan.max_file_bytes)
                          if settings.scan.include_header else None)
                f, why = _scan_file(llm, prompts[cat], rel, text, ruleset,
                                    settings.scan.max_tokens,
                                    timeout=settings.scan.per_file_timeout_s,
                                    temperature=settings.scan.temperature,
                                    header=header)
                return idx, rel, cat, f, why

            workers = max(1, settings.scan.workers)
            runs = runs_done
            # Every (file, rule group) unit is submitted once PER RUN, into the same
            # pool — so repeated runs parallelise with everything else rather than
            # serialising N whole passes.
            units = [(idx, rel, cat, run)
                     for run in range(runs)
                     for idx, rel in enumerate(targets, start=1)
                     for cat, _sub in groups]
            # Per-file failures are collected per category first: a file where two of
            # seven rule groups exhausted their budget was PARTLY analysed, and saying
            # which groups went unexamined is far more useful than dropping the file.
            per_file_fail: dict[str, list[str]] = {}
            seen_files: set[str] = set()
            # (file, rule, line) -> set of run indices that reported it.
            seen_in_runs: dict[tuple[str, str, int], set[int]] = {}
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(_one, u): u for u in units}
                for fut in as_completed(futures):
                    idx, rel, cat, run = futures[fut]
                    try:
                        idx, rel, cat, file_findings, failure = fut.result()
                    except Exception as exc:  # noqa: BLE001 - one unit must not kill the run
                        failure, file_findings = (
                            f"{exc.__class__.__name__}: {exc}", [])
                    if rel not in seen_files:
                        seen_files.add(rel)
                        scanned += 1
                    if failure:
                        per_file_fail.setdefault(rel, []).append(
                            f"{cat or 'all rules'}: {failure}")
                        log.warning("scan: [%d/%d] %s%s — NOT ANALYZED: %s", idx, total,
                                    rel, f" [{cat}]" if cat else "", failure)
                    else:
                        log.info("scan: [%d/%d] %s%s — %d finding(s)", idx, total, rel,
                                 f" [{cat}]" if cat else "", len(file_findings))
                    for f in file_findings:
                        seen_in_runs.setdefault((f.file, f.rule_id, f.line),
                                                set()).add(run)
                    findings.extend(file_findings)
            # Collapse by identity — file, line and rule — keeping the first. Two
            # separate things make this necessary: splitting the profile means one defect
            # can be reported under two categories (a bad memcpy is both BUF and MEM),
            # and running N times means the same finding arrives once per run. Either way
            # the report should name a defect once.
            findings[:] = _dedupe(findings, settings.scan.line_tolerance)
            # Attach how many of the N runs saw each finding. Individual runs do not
            # reproduce (quality/SCAN_REPRODUCIBILITY.md), so this fraction is the
            # confidence signal: 5-of-5 is almost certainly real, 1-of-5 almost certainly
            # sampling noise.
            for f in findings:
                f.runs_seen = len(seen_in_runs.get((f.file, f.rule_id, f.line), {0}))
                f.run_fraction = round(f.runs_seen / runs, 4)

            n_groups = len(groups) * runs
            for rel, why in per_file_fail.items():
                if len(why) >= n_groups:
                    # Nothing was applied: the file was never analysed at all, so any
                    # truncation is moot for COUNTING it — but still worth naming in the
                    # reason a reader sees, so fold it in rather than losing it.
                    reason = "; ".join(why)
                    if rel in truncated:
                        reason = f"{truncated[rel]}; {reason}"
                    failures[rel] = reason               # `failed` dominates `partial`
                else:
                    # Some rule groups ran and produced findings. Counting the file as
                    # wholly failed understated a real result: a live run applied six of
                    # seven groups and returned eleven correct findings, and still
                    # reported files_analyzed=0, files_failed=1. Partial is its own state
                    # — it belongs with truncation, not with "never examined".
                    unit = "rule group(s)" if runs == 1 else "rule group run(s)"
                    reason = (
                        f"{len(why)} of {n_groups} {unit} were not applied: "
                        + "; ".join(why))
                    if rel in truncated:
                        # Both causes name a real gap in coverage; overwriting one with
                        # the other silently discards a true reason a reader needs — the
                        # exact mistake that once sent someone to fix a setting (an
                        # excluded-files config) that had excluded nothing.
                        reason = f"{truncated[rel]}; {reason}"
                    partial[rel] = reason
            # Files that were truncated but suffered no rule-group failure at all (every
            # group ran) are still incompletely covered — the model just never saw the
            # rest of the file. They are not in `per_file_fail`, so they need composing
            # here, and only once `failures` is final so a wholly-failed file (handled
            # above) is never re-added to `partial` by this pass.
            for rel, note in truncated.items():
                if rel not in failures and rel not in partial:
                    partial[rel] = note
            used_llm_summary = _enrich(findings, store)
            if used_llm_summary:
                summary_model = store.get_meta("summary_model")
        finally:
            store.close()
    finally:
        if owns_llm and llm is not None:
            llm.close()

    # Drop findings too rare to trust — but never silently. `min_run_fraction` is 0 by
    # default so nothing is removed unless a user asks for it, and whatever IS removed is
    # counted, broken down by rule, and named in a warning. A threshold that quietly
    # deletes findings is the exact failure the coverage contract exists to prevent.
    filtered: dict[str, Any] = {}
    threshold = settings.scan.min_run_fraction
    if threshold > 0:
        dropped = [f for f in findings if f.run_fraction < threshold]
        if dropped:
            by_rule: dict[str, int] = {}
            for f in dropped:
                by_rule[f.rule_id] = by_rule.get(f.rule_id, 0) + 1
            findings[:] = [f for f in findings if f.run_fraction >= threshold]
            filtered = {"min_run_fraction": threshold, "runs": runs_done,
                        "removed": len(dropped), "by_rule": dict(sorted(by_rule.items()))}
            log.warning(
                "scan: %d finding(s) were seen in fewer than %.0f%% of %d run(s) and are "
                "NOT listed below (scan.min_run_fraction=%s). They are counted under "
                "'filtered' in scan.json — a rare finding is not the same as no finding.",
                len(dropped), threshold * 100, runs_done, threshold)

    summary = summarize(findings)
    summary["runs"] = runs_done
    # A clean report is only meaningful if every file was actually analyzed. Make an
    # incomplete scan impossible to mistake for an all-clear.
    #
    # `summary["files_analyzed"]` (here) and `coverage["files"]["succeeded"]` (built
    # below) deliberately disagree, and both stay in scan.json: `files_analyzed`
    # counts everything the model saw ANY output for and therefore INCLUDES partial
    # files (truncated reads, partly-applied rule groups), matching this field's
    # legacy meaning from before `Coverage` existed. `coverage.files.succeeded`
    # EXCLUDES partial files — a partial file is neither a clean success nor a
    # failure, and `Coverage` needs a state that means only "ran with nothing held
    # back". Redefining `files_analyzed` to exclude partials would have changed a
    # legacy key's meaning out from under existing consumers, which is exactly what
    # the additive-retrofit rule for this module forbids. The mapping between them is
    # `files_analyzed == succeeded + partial` (both exclude `failed`), i.e.
    # `scanned - len(failures) == (attempted - n_partial - n_failed) + n_partial`.
    summary["files_analyzed"] = scanned - len(failures)
    summary["files_failed"] = len(failures)
    summary["files_partial"] = len(partial)
    summary["files_eligible"] = eligible_total
    # A capability claim the tool can support for C and cannot for C++. The profile
    # declares `languages: [C, C++]`; on measured evidence that is true for one of them.
    # Warned rather than removed: dropping C++ from the profile would select zero files
    # and print "No findings against the configured rules" over an unread codebase,
    # which is the silent false all-clear this scanner exists to prevent.
    cpp_targets = sum(1 for rel in targets if detect_language(repo / rel) == "C++")
    summary["files_cpp"] = cpp_targets
    if cpp_targets:
        log.warning(
            "scan: %d of %d file(s) are C++, where this scanner is NOT recommended. "
            "Measured on PX4 GNSS parsers after the MEM-002/ISR-002 rule fixes: 24%% of "
            "findings are real defects (was 0%%). Precision improved; recall did not — a "
            "known defect surfaced in roughly 1 run in 3, and run frequency does not "
            "distinguish a real defect from a safe one. Treat C++ findings as leads to "
            "verify, and absence of findings as no evidence at all. "
            "See quality/PRECISION_CPP_POSTFIX.md.",
            cpp_targets, len(targets))
    # Derived from SELECTION, not from how many files later succeeded. Subtracting
    # `scanned` folded read failures into the cap and misnamed the cause.
    skipped = max(0, eligible_total - len(targets))
    summary["files_skipped_by_cap"] = skipped
    if skipped:
        # Without this a capped run and an exhaustive one produce identical-looking
        # "no findings" reports. On a safety scanner the difference is the whole point.
        log.warning(
            "scan: %d of %d eligible file(s) were NOT scanned (scan.max_files=%d). "
            "Findings are a lower bound over the whole repository.",
            skipped, eligible_total, settings.scan.max_files)
    if partial:
        log.warning(
            "scan: %d file(s) were only PARTIALLY analysed — the model never saw the "
            "rest, so a clean result for them covers only the part that was sent. %s",
            len(partial),
            "; ".join(f"{f}: {why}" for f, why in list(partial.items())[:3]),
        )
    if failures:
        log.warning(
            "scan: %d of %d file(s) were NOT analyzed — results are INCOMPLETE and a "
            "'no findings' result does not mean the code is clean. First failures: %s",
            len(failures), scanned,
            "; ".join(f"{f}: {why}" for f, why in list(failures.items())[:3]),
        )
    # `files` coverage: derived from the SAME `failures`/`partial`/`skipped_by_cap`
    # data the legacy summary keys above are derived from — never recomputed from a
    # second source, which is exactly how those keys drifted apart from each other
    # before this module existed.
    n_failed = len(failures)
    n_partial = len(partial)
    attempted = len(targets)
    files_why: dict[str, UnitState] = {}
    for rel, reason in failures.items():
        files_why[rel] = UnitState("failed", reason)
    # `partial` is written second, so a file present in both dicts would silently end
    # up shown here as `partial` only. That cannot happen today — the state-exclusivity
    # fix (see tests/test_coverage_state_exclusivity.py) makes `failures` and `partial`
    # disjoint by construction — but this ordering is only safe BECAUSE of that upstream
    # guarantee, not because of anything in this loop.
    for rel, reason in partial.items():
        files_why[rel] = UnitState("partial", reason)
    cap_reason = f"scan.max_files={settings.scan.max_files}"
    for rel in skipped_by_cap:
        files_why[rel] = UnitState("skipped", cap_reason)
    # `Coverage.__post_init__` raises on an internal accounting bug — a good property
    # for a test, but in production it must not destroy an otherwise-successful scan's
    # entire output (findings included) over a bug in the coverage bookkeeping alone.
    # On failure: log loudly and OMIT `coverage` entirely — never a partial or zeroed
    # block, which would be indistinguishable from a real one.
    try:
        coverage: dict[str, Coverage] | None = {
            "files": Coverage(
                eligible=eligible_total,
                attempted=attempted,
                succeeded=attempted - n_partial - n_failed,
                partial=n_partial,
                failed=n_failed,
                skipped=len(skipped_by_cap),
                why=files_why,
            )
        }
    except ValueError as exc:
        log.error(
            "scan: INTERNAL BUG — coverage accounting is inconsistent and will NOT be "
            "included in this run's report (this is a bug in secagent itself, not a "
            "problem with the scanned repository; the findings above are unaffected): "
            "eligible=%d attempted=%d succeeded=%d partial=%d failed=%d skipped=%d — %s",
            eligible_total, attempted, attempted - n_partial - n_failed, n_partial,
            n_failed, len(skipped_by_cap), exc)
        coverage = None

    # `Provenance` is built in its OWN guarded region, separate from `coverage`
    # above: a `Provenance.__post_init__` failure must not suppress the coverage
    # block (or vice versa) — each is independently useful even if the other is
    # absent. On failure: log loudly and OMIT `provenance` entirely, same discipline
    # as `coverage` — never a partial or placeholder block, which would be
    # indistinguishable from a real one.
    #
    # `model`: `llm.config.model` is the client scan_repo ITSELF used for every
    # per-file rule-group call, taken from the client in hand so an injected `llm=`
    # is reported truthfully rather than from `settings.llm.model` (which could
    # differ). `summary_model` is appended only when `_enrich` actually applied an
    # LLM-sourced cached summary to a finding — those summaries were produced at
    # INDEX time, possibly by a different model than this run's, which is exactly
    # why `model` is a list. `heuristic_only` is always False here: scan has no
    # heuristic-fallback path (UC4's whole design is LLM-based rule review), so a
    # model is always in hand and named — there is no case where scan produces
    # output "no model contributed".
    try:
        model = [llm.config.model]
        if used_llm_summary and summary_model and summary_model not in model:
            model.append(summary_model)
        provenance: Provenance | None = Provenance.now(
            model=model, endpoint=llm.config.base_url, heuristic_only=False)
    except ValueError as exc:
        log.error(
            "scan: INTERNAL BUG — provenance accounting is inconsistent and will NOT "
            "be included in this run's report (this is a bug in secagent itself, not a "
            "problem with the scanned repository; the findings above are unaffected): %s",
            exc)
        provenance = None

    md = render_markdown(findings, project=repo.name, ruleset_name=ruleset.name,
                         summary=summary, banner=settings.marking.banner)
    md_path = out / "scan.md"
    json_path = out / "scan.json"
    md_path.write_text(md, encoding="utf-8")
    scan_payload: dict[str, Any] = {
        "ruleset": ruleset.name, "summary": summary,
        "findings": [f.to_dict() for f in findings],
        "failures": failures, "partial": partial,
    }
    if filtered:
        scan_payload["filtered"] = filtered
    if coverage is not None:
        scan_payload["coverage"] = coverage_to_dict(coverage)
    if provenance is not None:
        scan_payload["provenance"] = provenance.to_dict()
    json_path.write_text(json.dumps(scan_payload, indent=2), encoding="utf-8")

    get_audit_logger(settings).record(
        "scan",
        target={"repo": str(repo), "ruleset": ruleset.name, "files_scanned": scanned,
                "findings": summary["total"], "critical": summary["critical"],
                "high": summary["high"]},
        model=settings.llm.model, endpoint=settings.llm.base_url,
    )

    result: dict[str, Any] = {
        "repo": str(repo),
        "ruleset": ruleset.name,
        "files_scanned": scanned,
        "files_failed": len(failures),
        # "Complete" means every eligible file was analysed in full. A capped run and a
        # truncated file are both incomplete over the repository, and a consumer reading
        # this flag to decide whether "no findings" means "clean" needs that distinction
        # — including when the incompleteness was deliberate.
        "analysis_complete": not failures and not partial and not skipped,
        "files_partial": len(partial),
        "files_skipped_by_cap": skipped,
        "out_dir": str(out),
        "report_md": str(md_path),
        "report_json": str(json_path),
        "summary": summary,
    }
    if coverage is not None:
        result["coverage"] = coverage_to_dict(coverage, include_why=False)
    if provenance is not None:
        # Unlike coverage's `why`, `provenance` is five small keys — no
        # context-flooding reason to withhold it from stdout, and stdout is exactly
        # where an agent evaluating this run's output would look for it.
        result["provenance"] = provenance.to_dict()
    return result


def _dedupe(findings: list[ScanFinding], line_tolerance: int = 0) -> list[ScanFinding]:
    """Collapse findings that name the same defect, keeping the first of each.

    Identity is ``(file, rule, line)``. With ``line_tolerance > 0`` a finding also
    matches an earlier one for the same file and rule within that many lines, and the
    earlier line wins.

    Exact matching is the default deliberately. The model drifts line numbers between
    runs — one measured rule group reported 66/70/76/79/84 across three runs of the same
    file, for what may be as few as two defects — so exact keying splits a wandering
    defect into several findings that each look rare. Merging by proximity would fix
    that, and would also fuse two genuinely distinct defects that happen to sit close
    together. Neither error is measured, so the tool does the conservative thing (never
    merge distinct defects) and the docstring on `runs_seen` says the frequency is a
    lower bound.
    """
    kept: list[ScanFinding] = []
    seen: set[tuple[str, int, str]] = set()
    for f in sorted(findings, key=lambda x: (x.file, x.rule_id, x.line)):
        key = (f.file, f.line, f.rule_id)
        if key in seen:
            continue
        if line_tolerance > 0 and any(
            k.file == f.file and k.rule_id == f.rule_id
            and abs(k.line - f.line) <= line_tolerance
            for k in kept
        ):
            continue
        seen.add(key)
        kept.append(f)
    return kept


_HEADER_EXTS = (".h", ".hpp", ".hh", ".hxx")
_INCLUDE_RE = re.compile(r'^\s*#\s*include\s+"([^"]+)"', re.M)


def _find_header(repo: Path, rel: str, max_bytes: int) -> tuple[str, str] | None:
    """The header that declares what ``rel`` uses, as ``(path, text)``.

    Same stem first (`sbf.cpp` -> `sbf.h`), then the first same-directory header the file
    `#include`s. Deliberately NOT a full include graph: resolving one hop covers the case
    that matters — a C++ class whose members are declared next door — and an include
    graph is a different piece of work with its own failure modes.
    """
    src = repo / rel
    for ext in _HEADER_EXTS:
        cand = src.with_suffix(ext)
        if cand.is_file():
            found = cand
            break
    else:
        found = None
        try:
            text = src.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        for inc in _INCLUDE_RE.findall(text):
            cand = (src.parent / inc).resolve()
            if cand.is_file() and cand.suffix.lower() in _HEADER_EXTS:
                found = cand
                break
    if found is None:
        return None
    try:
        htext = found.read_text(encoding="utf-8")[:max_bytes]
    except (OSError, UnicodeDecodeError):
        return None
    try:
        hrel = found.relative_to(repo).as_posix()
    except ValueError:
        hrel = found.name
    return hrel, htext


def _system_prompt(ruleset: RuleSet) -> str:
    # Take the language from the PROFILE. This said "C/C++ embedded-systems reviewer"
    # even when scanning Rust against rust-safety.yaml — so the rules were Rust-aware
    # while the persona framing told the model to think in C. On the language whose
    # entire safety argument differs from C's, that is not a cosmetic mismatch.
    langs = " / ".join(ruleset.languages) if ruleset.languages else "systems"
    return harden_system_prompt(
        f"You are a meticulous {langs} code reviewer specializing in "
        "memory safety and runtime stability. Review the provided source file ONLY "
        "against the rules below. Report concrete, line-anchored violations; do not "
        "invent issues or comment on style.\n\n"
        + rules_prompt(ruleset)
        + "\n\n"
        + _OUTPUT_INSTRUCTION
    )


def _scan_file(llm: LLMClient, sys_prompt: str, rel: str, text: str,
               ruleset: RuleSet, max_tokens: int = 0,
               timeout: float | None = None,
               temperature: float = 0.0,
               header: tuple[str, str] | None = None) -> tuple[list[ScanFinding], str]:
    """Scan one file. Returns ``(findings, failure_reason)``.

    ``failure_reason`` is empty on success. A scanner that cannot tell "I found no
    problems" from "I never managed to look" is worse than useless: a silent false
    all-clear on a memory-safety tool is the most dangerous output it can produce. Every
    way the model can fail to answer is reported, not swallowed.
    """
    parts = [f"File under review: {rel}\n\n{wrap_untrusted(_numbered(text), 'source')}"]
    if header:
        hrel, htext = header
        # Context, not subject. Unnumbered on purpose: the model has only one set of line
        # numbers to quote, so it cannot report a header line as a source line.
        parts.append(
            f"\nREFERENCE ONLY — {hrel}, the header for {rel}. It is provided so you can "
            f"see declarations, initialisers and types that {rel} uses but does not "
            f"declare. Do NOT report findings in {hrel}; every finding must be a line in "
            f"{rel}, using the numbering above.\n\n"
            f"{wrap_untrusted(htext, 'header')}"
        )
    parts.append("Findings (JSON array):")
    user = "\n\n".join(parts)
    try:
        resp = llm.chat(
            [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user}],
            max_tokens=max_tokens or _default_budget(llm.config),
            timeout=timeout,
            # Scan-specific, not the global `llm.temperature`. 0 means "follow the
            # global"; see `ScanConfig.temperature` for why rule-checking wants its own.
            temperature=temperature or None,
        )
    except Exception as exc:  # noqa: BLE001 - any model/transport failure
        return [], f"LLM call failed: {exc.__class__.__name__}: {exc}"
    if not (resp.content or "").strip():
        # A reasoning model that spends its whole budget on reasoning returns HTTP 200
        # with empty content; the client already retries at the largest budget the
        # context window allows, so reaching here means even that was not enough.
        #
        # Name the cause by what actually drives it. Reasoning cost scales with the size
        # of the input, and the retry ceiling comes from llm.context_window — pointing at
        # max_output_tokens sent people to a knob that cannot help, which is how this was
        # misread as a hard model limit rather than a budget left at its default.
        # Only blame size when size is plausibly the cause. A 3894-byte header ran a
        # model away for 10.5 minutes, and telling someone to lower a 12000-byte cap for
        # a file a third that size sends them to fix something that is not broken.
        big = len(text) > 8000
        hint = ("Lower scan.max_file_bytes"
                if big else
                "This file is small, so size is not the cause — the model did not "
                "converge on it. Try a different model, or exclude the file")
        return [], (
            f"model returned empty content after retry — it spent its whole output "
            f"budget reasoning about {len(text)} chars of source. {hint}."
        )
    if _find_json_array(resp.content) is None:
        return [], "model output contained no JSON array of findings"
    return parse_findings(resp.content, rel, ruleset), ""


def parse_findings(text: str, file: str, ruleset: RuleSet) -> list[ScanFinding]:
    """Parse the model's JSON array of findings, tolerantly."""
    blob = _find_json_array(text)
    if blob is None:
        return []
    try:
        items = json.loads(blob)
    except json.JSONDecodeError:
        return []
    if not isinstance(items, list):
        return []

    by_id = ruleset.by_id()
    out: list[ScanFinding] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        rule_id = str(it.get("rule") or it.get("rule_id") or it.get("id") or "").strip()
        rule = by_id.get(rule_id)
        severity = str(it.get("severity") or (rule.severity if rule else "medium")).lower()
        if not meets_threshold(severity, ruleset.min_severity):
            continue
        out.append(
            ScanFinding(
                rule_id=rule_id or "UNKNOWN",
                category=(rule.category if rule else str(it.get("category", "general"))),
                severity=severity,
                file=file,
                line=_to_int(it.get("line")),
                message=str(it.get("message") or it.get("explanation") or "").strip(),
            )
        )
    # Keep the most severe when a file exceeds the cap. This kept whatever order the
    # model happened to emit, so a file with 25 findings could drop a critical to keep an
    # info — the cap deciding severity by accident.
    out.sort(key=lambda f: SEVERITY_RANK.get(f.severity, 9))
    return out[: ruleset.max_findings_per_file]


def _select_files(
    repo: Path, settings: Settings, paths: list[str] | None, langs: set[str]
) -> tuple[list[str], list[str]]:
    """Return ``(files_to_scan, eligible_files)``.

    ``eligible_files`` is the full ranked population BEFORE the cap, not merely its
    count — a caller that only knows *how many* files were skipped can never say
    *which*. ``files_to_scan`` is always ``eligible_files``'s own prefix, so a caller
    can recover the skipped slice with ``eligible_files[len(files_to_scan):]`` rather
    than recomputing it from a second source.
    """
    cap = settings.scan.max_files          # 0 = the whole project
    if paths:
        return (paths[:cap] if cap > 0 else paths), paths
    eligible: list[str] = []
    for fpath in walk_files(
        repo, settings.affordances.ignore_globs, settings.affordances.ignore_vcs
    ):
        if detect_language(fpath) in langs:
            eligible.append(rel_posix(fpath, repo))
    # Walk to the end even when capped. Stopping early made the cap invisible: the report
    # could not say how many eligible files were never considered, so a bounded scan and
    # an exhaustive one produced indistinguishable "no findings" results.
    #
    # Rank before capping. `--max-files 6` used to take the first six in path order, so on
    # a repo with a `tests/` or `vendor/` directory sorting early the budget went there —
    # the same defect fixed separately in triage, testgen and docs. Reported by an
    # evaluation agent who had to point `repo` at a subdirectory to reach real logic.
    eligible.sort(key=path_rank)
    return (eligible[:cap] if cap > 0 else eligible), eligible


def _enrich(findings: list[ScanFinding], store) -> bool:
    """Attach component + file purpose to each finding.

    Returns whether an LLM-sourced summary (`FileSummary.source == "llm"`) was
    actually applied to at least one finding — `Provenance.model` in `scan_repo`
    only names the summary-time model when this is true, since summaries cached at
    index time can be heuristic-only even when this run's own LLM client works fine.
    """
    summaries = store.all_summaries()
    used_llm_summary = False
    for f in findings:
        f.component = component_for(f.file)
        s = summaries.get(f.file)
        if s:
            f.file_purpose = s.purpose
            if s.source == "llm":
                used_llm_summary = True
    return used_llm_summary


# Below this, an uncapped scan is quick enough that warning about it is just noise.
_COST_NOTICE_FILES = 20


def _default_budget(cfg: LLMConfig) -> int:
    """Output budget for a scan call when none is configured.

    Asking too small is expensive and asking too large is not: a reasoning model stops
    when it is done, so a generous cap buys no extra tokens and no extra time — it only
    stops truncating. Too small a cap costs a full wasted call (13-85s each, measured)
    plus the retry, on every single file.

    `max_output_tokens` defaults to 1024, which a reasoning model spends entirely on
    `reasoning_content` before emitting a character, so following it alone guaranteed
    that waste. Scale with the window instead, which is what actually bounds us.
    """
    return max(900, cfg.max_output_tokens, min(cfg.context_window // 4, 16384))


def _read_capped(path: Path, max_bytes: int) -> tuple[str, int] | None:
    """Return ``(text, full_size)``. ``len(text) < full_size`` means it was truncated.

    The caller needs the second number. Sending the model the first N bytes and then
    reporting the file as *analyzed* is the exact failure this suite keeps finding: a
    clean result covering 30% of a file is indistinguishable from a clean file.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    # Both numbers must be measured the same way. Comparing stat() BYTES against len()
    # CHARACTERS made every file holding one non-ASCII character look truncated, which
    # would have put a "partial coverage" warning on scans that were complete — and a
    # warning that always fires is one nobody reads.
    full = len(text)
    return (text[:max_bytes], full) if full > max_bytes else (text, full)


def _numbered(text: str) -> str:
    return "\n".join(f"{i}: {ln}" for i, ln in enumerate(text.splitlines(), start=1))


def _find_json_array(text: str) -> str | None:
    fence = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    if fence:
        return fence.group(1)
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end > start:
        return text[start : end + 1]
    return None


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
