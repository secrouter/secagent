"""UC3 orchestration: run/ingest IKOS, enrich, triage, and report."""

from __future__ import annotations

import bisect
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from ...affordances import grounding, queries
from ...affordances.io_map import component_for
from ...affordances.priority import path_rank
from ...audit import get_audit_logger
from ...config import Settings
from ...llm.client import LLMClient
from ...output import Coverage, Provenance, UnitState, coverage_to_dict
from ...sanitize import harden_system_prompt, wrap_untrusted
from ...security import enforce_fips_policy
from .ikos import (
    compile_flags_for,
    enumerate_functions,
    iter_compile_units,
    parse_ikos_report,
    run_ikos,
)
from .models import SEVERITY_ORDER, Finding
from .report import render_markdown, summarize

log = logging.getLogger(__name__)


def analyze_repo(
    repo: str | Path,
    settings: Settings,
    *,
    target: str | Path | None = None,
    ikos_report: str | Path | None = None,
    out_dir: str | Path | None = None,
    run: bool = True,
    llm: LLMClient | None = None,
) -> dict[str, Any]:
    """Analyze C/C++ with IKOS and produce an enriched report.

    Provide ``ikos_report`` to ingest an existing IKOS JSON report (works without the
    IKOS binary), or ``target`` with ``run=True`` to invoke IKOS on a file/.bc.
    """
    repo = Path(repo).resolve()
    enforce_fips_policy(settings.fips.require_fips, settings.fips.allow_non_fips)

    out = Path(out_dir).resolve() if out_dir else (repo / "secagent-analysis")
    out.mkdir(parents=True, exist_ok=True)

    # 1. Obtain findings (ingest a report, or run IKOS).
    findings: list[Finding]
    if ikos_report is not None:
        findings = parse_ikos_report(Path(ikos_report).read_text(encoding="utf-8"))
        source = str(ikos_report)
    elif run and target is not None:
        report_path = out / "ikos-report.json"
        cfg = settings.analysis
        # Compile flags: explicit, else looked up from a compile_commands.json (cFS-style).
        flags = list(cfg.compile_flags) or (
            compile_flags_for(cfg.compile_db, target) if cfg.compile_db else [])
        # Entry points: explicit, else enumerate the TU's functions for a library (no main).
        eps = list(cfg.entry_points) or (
            enumerate_functions(target, flags) if cfg.library_mode else [])
        # A failed IKOS run must not take the command down with it. `analyze run
        # --library` on a C++ file with a defaulted constructor raised straight out of
        # here as an unhandled traceback: entry-point enumeration compiles the TU
        # separately from IKOS's own preprocessing, the two disagree about which symbols
        # survive, and IKOS treats an entry point it cannot find as fatal. `analyze scan`
        # has always degraded per-TU (see `_scan_one`); the single-target path was the
        # one place that did not, so the same underlying failure produced a clean
        # "skipped, here is the diagnostic" in one command and a stack trace in the other.
        tu_error = ""
        findings = []
        source = str(target)
        try:
            run_ikos(
                target, report_path=report_path,
                status_filter=cfg.status_filter, analyses_filter=cfg.analyses_filter,
                domain=cfg.domain, procedural=cfg.procedural, no_pointer=cfg.no_pointer,
                compile_flags=flags, entry_points=eps,
            )
            findings = parse_ikos_report(report_path.read_text(encoding="utf-8"))
        except (RuntimeError, OSError, ValueError) as exc:
            tu_error = _diagnostic(str(exc)) or str(exc)[:300]
            log.warning(
                "analysis: IKOS did not analyse %s — NO findings were produced for it, "
                "which is NOT the same as finding none. %s%s",
                target, tu_error,
                (f" (attempted with {len(eps)} entry point(s))" if eps else ""))
        tu_kwargs = {
            "eligible": 1, "attempted": 1,
            "succeeded": 0 if tu_error else 1,
            "partial": 0, "failed": 1 if tu_error else 0, "skipped": 0,
            "why": ({_relativize(str(target), repo): UnitState("failed", tu_error)}
                    if tu_error else {}),
        }
        return _finalize(repo, settings, findings, out, source, llm=llm,
                         extra_coverage_kwargs={"translation_units": tu_kwargs})
    else:
        raise ValueError("provide ikos_report=<path> (ingest) or target=<file> with run=True")

    return _finalize(repo, settings, findings, out, source, llm=llm)


def analyze_scan(
    repo: str | Path,
    settings: Settings,
    *,
    compile_db: str | Path = "",
    out_dir: str | Path | None = None,
    jobs: int = 0,
    per_tu_timeout: int = 120,
    max_files: int = 0,
    llm: LLMClient | None = None,
) -> dict[str, Any]:
    """Run IKOS across every C/C++ TU in a compile database and aggregate the findings.

    Each TU is analyzed independently (parallel, ``jobs`` workers) with a per-TU timeout so
    one slow/pathological unit can't stall the whole scan. Use the ``analysis`` config's
    domain/no_pointer knobs to trade precision for speed (see :class:`AnalysisConfig`).
    """
    repo = Path(repo).resolve()
    enforce_fips_policy(settings.fips.require_fips, settings.fips.allow_non_fips)
    out = Path(out_dir).resolve() if out_dir else (repo / "secagent-analysis")
    out.mkdir(parents=True, exist_ok=True)
    cfg = settings.analysis

    db = str(compile_db or cfg.compile_db) or _discover_compile_db(repo)
    if not db:
        raise ValueError(
            "no compile_commands.json found — pass --compile-db, or build the project with "
            "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON so IKOS gets each file's include/define flags."
        )
    # The PRE-cap count. `iter_compile_units` before any slicing is the only honest
    # `eligible` for this domain — capturing it after the slice (as `len(units)`
    # already does, for the legacy `tus_total`) is exactly the "reported 0 skipped
    # for files that were never considered" defect this coverage block exists to
    # catch: a bounded run's `tus_total` is POST-cap, so a naive `tus_skipped` over
    # it always reads 0 no matter how many TUs the cap actually excluded.
    #
    # Ranked before the cap, same as `_select_files` in scan and `_gen_unit` in
    # testgen: `iter_compile_units` yields TUs in compile-database order, which is
    # "whatever sorts first" wearing a different hat — the same defect that sent the
    # other four commands' budgets to `cFS/osal/` headers and `examples/*` scripts.
    # `path_rank`'s own path tiebreaker keeps this deterministic across runs.
    all_units = sorted(iter_compile_units(db), key=lambda u: path_rank(u[0]))
    units = all_units[:max_files] if max_files else all_units
    cap_skipped_units = all_units[max_files:] if max_files else []
    if not units:
        raise ValueError(f"no C/C++ translation units in {db}")

    ikos_dir = out / "ikos"
    ikos_dir.mkdir(parents=True, exist_ok=True)

    def _scan_one(item: tuple[str, list[str]]) -> tuple[str, list[Finding], str | None]:
        tu, flags = item
        eps = list(cfg.entry_points) or (
            enumerate_functions(tu, flags) if cfg.library_mode else [])
        rp = ikos_dir / (_safe_name(tu) + ".sarif")
        try:
            run_ikos(
                tu, report_path=rp, status_filter=cfg.status_filter,
                analyses_filter=cfg.analyses_filter, domain=cfg.domain,
                procedural=cfg.procedural, no_pointer=cfg.no_pointer,
                compile_flags=flags, entry_points=eps, timeout=per_tu_timeout,
            )
            return tu, parse_ikos_report(rp.read_text(encoding="utf-8")), None
        except (RuntimeError, OSError) as exc:
            return tu, [], str(exc)

    workers = jobs if jobs > 0 else min(8, (os.cpu_count() or 2))
    findings: list[Finding] = []
    skipped: list[tuple[str, str]] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for tu, fs, err in ex.map(_scan_one, units):
            if err is not None:
                skipped.append((tu, err))
            else:
                findings.extend(fs)

    stats = {
        "tus_total": len(units),
        "tus_analyzed": len(units) - len(skipped),
        "tus_skipped": len(skipped),
        # The honest population, added alongside — not in place of — the legacy
        # `tus_total`, which is POST-cap and therefore cannot itself say how many
        # TUs the cap excluded.
        "tus_eligible": len(all_units),
        "jobs": workers, "per_tu_timeout_s": per_tu_timeout,
        "domain": cfg.domain or "default", "no_pointer": cfg.no_pointer,
        "skipped_sample": [
            {"file": _relativize(f, repo), "error": _diagnostic(err)}
            for f, err in skipped[:5]
        ],
    }

    tu_why: dict[str, UnitState] = {}
    for tu, err in skipped:
        reason = _diagnostic(err) or "IKOS analysis failed (no diagnostic captured)"
        tu_why[_relativize(tu, repo)] = UnitState("failed", reason)
    if max_files:
        cap_reason = f"--max-files={max_files}"
        for tu, _flags in cap_skipped_units:
            tu_why[_relativize(tu, repo)] = UnitState("skipped", cap_reason)
    # Passed as raw kwargs, not a constructed `Coverage`, so the (possibly
    # inconsistency-raising) construction happens inside `_finalize`'s single guarded
    # region alongside `triage` — never before it, where a `ValueError` here would
    # crash the whole command before a single finding was written to disk.
    tu_coverage_kwargs = {
        "eligible": len(all_units),
        "attempted": len(units),
        "succeeded": len(units) - len(skipped),
        "partial": 0,
        "failed": len(skipped),
        "skipped": len(all_units) - len(units),
        "why": tu_why,
    }
    return _finalize(repo, settings, findings, out, db, llm=llm, scan_stats=stats,
                     extra_coverage_kwargs={"translation_units": tu_coverage_kwargs})


def _diagnostic(err: str) -> str:
    """The line a human needs from a compiler/analyser failure.

    Keeping `splitlines()[-1]` threw away the actual diagnostic and preserved a generic
    trailer ("ikos: error while compiling X, abort."), which says only that something
    failed — turning a fixable missing-include into an unexplained one.
    """
    lines = [ln.strip() for ln in err.splitlines() if ln.strip()]
    for line in lines:
        if "error:" in line or "fatal error" in line.lower():
            return line[:300]
    return (lines[-1] if lines else "")[:300]


def _discover_compile_db(repo: Path) -> str:
    """Find a compile_commands.json under ``repo`` (prefer the largest by entry count)."""
    best, best_n = "", -1
    for p in repo.glob("**/compile_commands.json"):
        try:
            n = len(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, TypeError):
            continue
        if n > best_n:
            best, best_n = str(p), n
    return best


def _safe_name(path: str) -> str:
    return path.strip("/").replace("/", "__").replace("\\", "__")


def _finalize(
    repo: Path,
    settings: Settings,
    findings: list[Finding],
    out: Path,
    source: str,
    *,
    llm: LLMClient | None = None,
    scan_stats: dict[str, Any] | None = None,
    extra_coverage_kwargs: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Enrich findings, optionally LLM-triage them, and write the report."""
    owns_llm = False
    if llm is None and settings.affordances.llm_summaries and settings.analysis.max_triage:
        from ...netpolicy import enforce

        enforce(settings)
        llm = LLMClient(settings.llm)
        owns_llm = True
    triage_stats: dict[str, Any] = {}
    # Emitted ALWAYS, even when triage never ran at all — `triage_stats == {}` used to
    # mean either "nothing to add" or "every call failed" with no way to tell them
    # apart, which is the exact defect the triage-health work exists to fix.
    triage_ran = llm is not None and bool(settings.analysis.max_triage)
    # Populated inside the indexed-store region below, before it closes — see the
    # matching comment in scan_repo/generate_tests for why `Provenance` is built
    # once the store is already gone.
    used_llm_summary = False
    summary_model: str | None = None
    try:
        store = queries.ensure_indexed(repo, settings)
        try:
            used_llm_summary = _enrich(findings, store, repo)
            if used_llm_summary:
                summary_model = store.get_meta("summary_model")
            if triage_ran:
                assert llm is not None  # implied by `triage_ran`; narrows for mypy
                triage_stats = _triage(findings, store, llm,
                                       settings.analysis.max_triage)
        finally:
            store.close()
    finally:
        if owns_llm and llm is not None:
            llm.close()

    summary = summarize(findings)
    # Surface triage health in the report itself. "No assessment" must not be able to
    # mean either "nothing to add" or "every call failed".
    if triage_stats:
        summary["triage"] = triage_stats

    # Every `Coverage` this command reports — `translation_units` (passed in as raw
    # kwargs from `analyze_scan`) and `triage` (built here) — is constructed inside
    # ONE guarded region. `Coverage.__post_init__` raises on an internal accounting
    # bug (e.g. a future regression of the state-exclusivity invariant), and that is
    # a good property for a test to catch — but in production it must not destroy an
    # otherwise-successful run's entire output. On failure, log loudly and omit
    # `coverage` entirely (never a partial or zeroed block: an absent key is
    # detectable by a consumer, a wrong one is not) while every legacy key and the
    # findings themselves are still written normally.
    domains: dict[str, Coverage] = {}
    coverage_ok = True
    try:
        for name, kwargs in (extra_coverage_kwargs or {}).items():
            domains[name] = Coverage(**kwargs)
        domains["triage"] = Coverage(**_triage_coverage_kwargs(
            findings, triage_ran, triage_stats, settings))
    except ValueError as exc:
        log.error(
            "analyze: INTERNAL BUG — coverage accounting is inconsistent and will NOT "
            "be included in this run's report (this is a bug in secagent itself, not a "
            "problem with the analyzed code; the findings above are unaffected): %s",
            exc)
        coverage_ok = False

    # `Provenance` is built in its OWN guarded region, separate from `coverage`
    # above, so a failure in one never suppresses the other.
    #
    # `model`: `llm.config.model` is included ONLY when `triage_ran` — that is the
    # one place `_finalize` actually uses `llm` for a call of its own (`_triage`);
    # taken from the client in hand, not `settings.llm.model`, so an injected `llm=`
    # is reported truthfully. `summary_model` is appended only when `_enrich`
    # actually applied an LLM-sourced cached summary to a finding.
    #
    # `heuristic_only` is unconditionally False, in EVERY case, including
    # `model == []`. This is the case most likely to be got wrong: an IKOS report
    # ingested with triage off has no model in the list (nothing in `model`
    # contributed), but the findings still came from IKOS — a real static analyzer —
    # not from secagent's heuristic fallback (analyze has no such fallback; findings
    # are always either ingested from a real report or produced by actually running
    # IKOS). Reporting `heuristic_only=True` here would tell a reader the findings
    # were guessed, which is false. `model=[]` + `heuristic_only=False` is exactly
    # the combination `Provenance` exists to allow for this reason.
    try:
        model: list[str] = []
        if triage_ran:
            assert llm is not None  # implied by `triage_ran`; narrows for mypy
            model.append(llm.config.model)
        if used_llm_summary and summary_model and summary_model not in model:
            model.append(summary_model)
        endpoint = llm.config.base_url if llm is not None else settings.llm.base_url
        provenance: Provenance | None = Provenance.now(
            model=model, endpoint=endpoint, heuristic_only=False)
    except ValueError as exc:
        log.error(
            "analyze: INTERNAL BUG — provenance accounting is inconsistent and will "
            "NOT be included in this run's report (this is a bug in secagent itself, "
            "not a problem with the analyzed code; the findings above are "
            "unaffected): %s", exc)
        provenance = None

    md = render_markdown(findings, project=repo.name, summary=summary,
                         banner=settings.marking.banner)
    md_path = out / "analysis.md"
    json_path = out / "analysis.json"
    md_path.write_text(md, encoding="utf-8")
    payload: dict[str, Any] = {"summary": summary,
                               "findings": [f.to_dict() for f in findings]}
    if scan_stats is not None:
        payload["scan"] = scan_stats
    if coverage_ok:
        payload["coverage"] = coverage_to_dict(domains)
    if provenance is not None:
        payload["provenance"] = provenance.to_dict()
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    get_audit_logger(settings).record(
        "analyze",
        target={"repo": str(repo), "source": source, "findings": summary["total"],
                "high": summary["high"], "medium": summary["medium"]},
        model=settings.llm.model if llm is not None else "",
        endpoint=settings.llm.base_url if llm is not None else "",
    )
    result = {
        "repo": str(repo), "source": source, "out_dir": str(out),
        "report_md": str(md_path), "report_json": str(json_path), "summary": summary,
    }
    if scan_stats is not None:
        result["scan"] = scan_stats
    if coverage_ok:
        result["coverage"] = coverage_to_dict(domains, include_why=False)
    if provenance is not None:
        # Unlike coverage's `why`, `provenance` is five small keys — no
        # context-flooding reason to withhold it from stdout.
        result["provenance"] = provenance.to_dict()
    return result


def _triage_coverage_kwargs(
    findings: list[Finding], triage_ran: bool, triage_stats: dict[str, Any],
    settings: Settings,
) -> dict[str, Any]:
    """Build the ``triage`` coverage domain's kwargs — ALWAYS, so the domain is present
    even when triage did not run at all (the exact case the triage-health work exists
    to make legible, not silent)."""
    n_findings = len(findings)
    if not triage_ran:
        reason = ("analysis.max_triage=0" if not settings.analysis.max_triage
                  else "no model endpoint configured")
        not_run_why = {f"{f.file}:{f.line}": UnitState("skipped", reason)
                       for f in findings}
        return {"eligible": n_findings, "attempted": 0, "succeeded": 0, "partial": 0,
                "failed": 0, "skipped": n_findings, "why": not_run_why}

    ran_why: dict[str, UnitState] = {}
    for unit_id, reason in triage_stats.get("failure_why", {}).items():
        ran_why[unit_id] = UnitState("failed", reason)
    skip_reason = f"analysis.max_triage={settings.analysis.max_triage}"
    for unit_id in triage_stats.get("skipped_ids", []):
        ran_why[unit_id] = UnitState("skipped", skip_reason)
    attempted = triage_stats["attempted"]
    return {
        "eligible": n_findings,
        "attempted": attempted,
        "succeeded": triage_stats["succeeded"],
        "partial": 0,
        "failed": triage_stats["failed"],
        "skipped": n_findings - attempted,
        "why": ran_why,
    }


def _containing_function(starts: list[tuple[int, str]], line: int) -> str:
    """The function containing ``line``, given ``(start_line, name)`` pairs in order.

    The nearest preceding function start is the answer, with no end-line bound needed: an
    IKOS finding is always reported at an executable statement, and an executable
    statement is always inside a function body. Nothing to bound it against can sit
    between a function's start and a statement it contains.
    """
    if not starts or line <= 0:
        return ""
    i = bisect.bisect_right(starts, (line, "￿")) - 1
    return starts[i][1] if i >= 0 else ""


def _enrich(findings: list[Finding], store, repo: Path) -> bool:
    """Attach component, file purpose and the containing function to each finding.

    ``Finding.function`` is resolved HERE, from the symbol index, rather than in the SARIF
    parser. The parser used to take it from ``stacks[]``, whose frames are spelled "Call
    from <f>" — so it named a CALLER of the containing function, one level out from where
    the defect actually is. Checked against source on cFS `fm_cmd_utils.c`, 10 of 10
    stacked findings pointed at a function that does not contain the flagged line (lines
    147-193 are inside `FM_GetFilenameState`; all were attributed to `FM_VerifyFileState`
    or `FM_VerifyNameValid`, which merely call it). That is worse than the empty field it
    replaced — an empty one is ignored, a wrong one is followed.

    The index already knows every function's start line, and `file:line` is what the
    finding is anchored to, so deriving it here needs no reinterpretation of anyone else's
    stack semantics. The call path itself is kept, under `called_from`.

    An existing value is not overwritten: the raw-JSON parser gets `function` from IKOS's
    own field, which is a direct statement of the containing function rather than a
    reading of a call stack.

    Returns whether an LLM-sourced summary (`FileSummary.source == "llm"`) was
    actually applied to at least one finding — see the parallel comment on
    `scan`'s `_enrich`. `Provenance.model` in `_finalize` only names the
    summary-time model when this is true.
    """
    summaries = store.all_summaries()
    used_llm_summary = False
    starts_by_file: dict[str, list[tuple[int, str]]] = {}
    for f in findings:
        rel = _relativize(f.file, repo)
        f.file = rel
        f.component = component_for(rel)
        if not f.function:
            if rel not in starts_by_file:
                starts_by_file[rel] = sorted(
                    (s.lineno, s.name) for s in store.symbols_for_file(rel)
                    if s.kind in ("function", "method") and s.lineno)
            f.function = _containing_function(starts_by_file[rel], f.line)
        s = summaries.get(rel)
        if s:
            f.file_purpose = s.purpose
            if s.source == "llm":
                used_llm_summary = True
    return used_llm_summary


def _triage_order(f: Finding) -> tuple:
    """Severity first, then the user's own code (see affordances.priority).

    `sort_key` breaks severity ties alphabetically, which sent the whole triage budget to
    vendored `cFS/osal/` headers — they sort before `fsw/src/`.
    """
    return (SEVERITY_ORDER.get(f.severity, 5), *path_rank(f.file))


def _triage(findings: list[Finding], store, llm: LLMClient, max_triage: int) -> dict[str, Any]:
    sys = harden_system_prompt(
        "You are a C/C++ security/static-analysis triager. Given a static-analysis "
        "finding and the surrounding code, give a ONE-sentence assessment: is it likely "
        "a true positive or false positive, and the key reason. Be concise and concrete."
    )
    attempted = succeeded = 0
    failures: list[str] = []
    # `failures` (above) is truncated to 10 for the legacy dict below; `failure_why`
    # carries every one of them, keyed by unit id, for the `triage` coverage domain's
    # `why` — built from a SEPARATE dict here rather than re-sliced or re-parsed from
    # `failures`, so the coverage `why` is never lossy where the legacy field is.
    failure_why: dict[str, str] = {}
    ungrounded: list[str] = []
    ordered = sorted(findings, key=_triage_order)
    triaged, budget_skipped = ordered[:max_triage], ordered[max_triage:]
    for f in triaged:
        attempted += 1
        start = max(1, f.line - 8)
        end = f.line + 8
        snippet = queries.read_slice(store, f.file, start, end)
        user = (
            f"Finding: [{f.checker}/{f.status}] {f.message} at {f.file}:{f.line}"
            f"{(' in ' + f.function) if f.function else ''}\n\n"
            f"Code:\n{wrap_untrusted(snippet, 'code')}\n\nAssessment:"
        )
        try:
            resp = llm.chat(
                [{"role": "system", "content": sys}, {"role": "user", "content": user}],
                # No hardcoded 120. A reasoning model spends far more than that on
                # `reasoning_content` before emitting a character, so a tight cap
                # returned empty content and every finding came back untriaged — which,
                # with the blanket `except` below, looked exactly like "nothing to say".
            )
            if resp.content.strip():
                assessment = resp.content.strip()
                f.triage = {"assessment": assessment}
                # Check the note against the index before presenting it as analysis.
                # Triage notes were observed asserting specific, checkable things that
                # were simply untrue ("the code explicitly checks its value" about code
                # that never does). Any path or backticked symbol it names either exists
                # in this repo or it does not, and that costs no model call to find out.
                g = grounding.check(store, assessment)
                if not g.ok:
                    f.triage["grounding"] = g.note()
                    ungrounded.append(f"{f.file}:{f.line}")
                succeeded += 1
            else:
                unit_id = f"{f.file}:{f.line}"
                failures.append(f"{unit_id}: model returned empty content")
                failure_why[unit_id] = "model returned empty content"
        except Exception as exc:  # noqa: BLE001 - triage is best-effort, never fatal
            # Best-effort must not mean invisible. Two evaluation agents each spent a
            # run believing triage had nothing to add, when in fact every call was
            # failing — an unreachable endpoint and a wrong model name — and the report
            # was byte-identical to a healthy run with nothing to report.
            unit_id = f"{f.file}:{f.line}"
            reason = f"{exc.__class__.__name__}: {exc}"
            failures.append(f"{unit_id}: {reason}")
            failure_why[unit_id] = reason
    if failures:
        log.warning(
            "analysis: LLM triage failed for %d of %d finding(s) — those findings are "
            "UNTRIAGED, not judged unremarkable. First: %s",
            len(failures), attempted, "; ".join(failures[:3]))
    if ungrounded:
        log.warning(
            "analysis: %d triage note(s) reference paths or symbols that are not in the "
            "index — they are marked UNVERIFIED in the report: %s",
            len(ungrounded), ", ".join(ungrounded[:3]))
    return {"attempted": attempted, "succeeded": succeeded,
            "failed": len(failures), "failures": failures[:10],
            "ungrounded": len(ungrounded),
            # Additive, internal-use keys for the `triage` coverage domain — not
            # asserted on by any existing test, so adding them here cannot regress
            # the legacy shape.
            "failure_why": failure_why,
            "skipped_ids": [f"{f.file}:{f.line}" for f in budget_skipped]}


def _relativize(file: str, repo: Path) -> str:
    p = Path(file)
    if p.is_absolute():
        try:
            return p.resolve().relative_to(repo).as_posix()
        except ValueError:
            return file
    return file
