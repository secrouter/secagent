"""UC5 orchestration: generate unit + functional tests into a separate folder."""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from ...affordances import queries
from ...affordances.call_map import is_test_path
from ...affordances.io_map import summarize_io
from ...affordances.languages import is_code
from ...affordances.priority import path_rank
from ...audit import get_audit_logger
from ...config import Settings
from ...llm.client import LLMClient
from ...output import Coverage, Provenance, UnitState, coverage_to_dict
from ...sanitize import harden_system_prompt, wrap_untrusted
from ...security import enforce_fips_policy
from .frameworks import ASSERT_MAIN, detect_framework
from .models import GeneratedTest

_EXT = {
    "Python": "py", "C": "c", "C++": "cpp", "JavaScript": "js", "TypeScript": "ts",
    "Go": "go", "Java": "java", "Rust": "rs", "C#": "cs", "Ruby": "rb",
}

_UC1_RECOMMENDATION = (
    "Run UC1 first for best results: `secagent docs build <repo>` (or at least "
    "`secagent index <repo>`) so test generation has rich file summaries and an IO map."
)


log = logging.getLogger(__name__)

def generate_tests(
    repo: str | Path,
    settings: Settings,
    *,
    out_dir: str | Path | None = None,
    unit: bool | None = None,
    functional: bool | None = None,
    llm: LLMClient | None = None,
    verify: bool = False,
) -> dict[str, Any]:
    """Generate unit and functional tests from the UC1 affordance store.

    ``verify=True`` runs the mechanical gates (`secagent.verify`) over everything written:
    does it compile, does it pass against correct code, and does it FAIL against
    deliberately broken code. Anything judged `vacuous` or `wrong` is moved to
    ``quarantine/`` — kept, because deleting it destroys the evidence, but somewhere its
    path alone says it is not a test.
    """
    repo = Path(repo).resolve()
    enforce_fips_policy(settings.fips.require_fips, settings.fips.allow_non_fips)
    cfg = settings.testgen
    do_unit = cfg.unit if unit is None else unit
    do_functional = cfg.functional if functional is None else functional

    # Output goes to a NEW top-level folder, separate from the project structure.
    out = Path(out_dir).resolve() if out_dir else (repo / cfg.out_dir)
    (out / "unit").mkdir(parents=True, exist_ok=True)
    (out / "functional").mkdir(parents=True, exist_ok=True)

    owns_llm = False
    if llm is None:
        from ...netpolicy import enforce

        enforce(settings)
        llm = LLMClient(settings.llm)
        owns_llm = True

    generated: list[GeneratedTest] = []
    recommendations: list[str] = [_UC1_RECOMMENDATION]
    unit_results: list[GeneratedTest] = []
    unit_skipped: list[str] = []
    func_results: list[GeneratedTest] = []
    func_skipped: list[str] = []
    # Detection reads the repo tree once per language, not once per file — populated
    # (serially, before any threading) by `_gen_unit`/`_gen_functional` on first use of
    # each language and shared between both passes.
    framework_cache: dict[str, tuple[str, bool]] = {}
    # Populated inside the indexed-store region below, before it closes — see the
    # matching comment in scan_repo for why `Provenance` is built once the store is
    # already gone.
    used_llm_summary = False
    summary_model: str | None = None
    try:
        store = queries.ensure_indexed(repo, settings)
        try:
            summaries = store.all_summaries()
            # If no LLM-backed summaries exist, UC1 was not (fully) run — recommend it.
            if not any(s.source == "llm" for s in summaries.values()):
                recommendations.append(
                    "The affordance store has no LLM summaries yet; running UC1 with a "
                    "model endpoint will materially improve generated tests."
                )
            if do_unit:
                unit_results, unit_skipped, unit_used_summary = _gen_unit(
                    repo, out, store, summaries, cfg, llm, framework_cache)
                generated += unit_results
                used_llm_summary = used_llm_summary or unit_used_summary
            if do_functional:
                func_results, func_skipped, func_used_summary = _gen_functional(
                    repo, out, store, summaries, cfg, llm, framework_cache)
                generated += func_results
                used_llm_summary = used_llm_summary or func_used_summary
            if used_llm_summary:
                summary_model = store.get_meta("summary_model")
        finally:
            store.close()
    finally:
        if owns_llm and llm is not None:
            llm.close()

    verification: list[dict[str, Any]] = []
    if verify:
        verification = _verify_written(repo, out, generated)

    # Disclose every language whose framework was ASSUMED (no repo evidence — see
    # `frameworks.detect_framework`), not silently presented as fact. A wrong framework
    # nobody in the repo actually uses is how a whole generated suite becomes
    # unrunnable, so this belongs next to the UC1 recommendation, not buried in
    # per-test JSON only.
    assumed = sorted({
        (g.language, g.framework) for g in generated if g.framework_assumed and g.framework
    })
    if assumed:
        detail = ", ".join(f"{lang}: {fw}" for lang, fw in assumed)
        recommendations.append(
            "No repo evidence was found for the test framework of these languages — "
            f"the configured default was ASSUMED, not observed, and may not match what "
            f"this project actually uses: {detail}. Generated tests for these languages "
            "may not run as-is; set testgen.frameworks to override, or add the real "
            "framework's usual markers (e.g. a conftest.py, a vendored gtest/Unity "
            "directory, a PackageReference) so secagent can detect it."
        )

    # One coverage domain per population actually run — `components` is omitted
    # entirely when the functional pass did not run, rather than reported with a
    # fabricated eligible count for a population that was never even looked at.
    # Built in its OWN guarded region, separate from generation above: a
    # `Coverage.__post_init__` accounting failure must not retroactively make it look
    # like generation itself failed, and must not stop the manifest/tests that WERE
    # produced from being written.
    domains: dict[str, Coverage] = {}
    coverage_ok = True
    try:
        if do_unit:
            domains["unit_files"] = _domain_coverage(
                unit_results, unit_skipped, f"testgen.max_unit_files={cfg.max_unit_files}")
        if do_functional:
            domains["components"] = _domain_coverage(
                func_results, func_skipped,
                f"testgen.max_functional_components={cfg.max_functional_components}")
    except ValueError as exc:
        log.error(
            "testgen: INTERNAL BUG — coverage accounting is inconsistent and will NOT "
            "be included in this run's manifest (this is a bug in secagent itself, not a "
            "problem with the generated tests; the tests above are unaffected): %s", exc)
        coverage_ok = False

    # `Provenance` is built in its OWN guarded region, separate from `coverage`
    # above, so a failure in one never suppresses the other.
    #
    # `model`: `llm.config.model` is the client `generate_tests` itself used for
    # every unit/functional generation call, taken from the client in hand (not
    # `settings.llm.model`) so an injected `llm=` is reported truthfully.
    # `summary_model` is appended only when `_gen_unit`/`_gen_functional` actually
    # fed an LLM-sourced cached summary into a generation prompt — those summaries
    # were produced at INDEX time, possibly by a different model than this run's.
    # `heuristic_only` is always False: testgen has no heuristic-fallback path (UC5
    # generates every test via the model), so a model is always in hand and named.
    try:
        model = [llm.config.model]
        if used_llm_summary and summary_model and summary_model not in model:
            model.append(summary_model)
        provenance: Provenance | None = Provenance.now(
            model=model, endpoint=llm.config.base_url, heuristic_only=False)
    except ValueError as exc:
        log.error(
            "testgen: INTERNAL BUG — provenance accounting is inconsistent and will "
            "NOT be included in this run's manifest (this is a bug in secagent itself, "
            "not a problem with the generated tests; the tests above are unaffected): "
            "%s", exc)
        provenance = None

    _write_manifest(out, repo, generated, recommendations, settings.marking.banner,
                    domains if coverage_ok else None, provenance)

    get_audit_logger(settings).record(
        "testgen",
        target={"repo": str(repo), "out_dir": str(out),
                "unit": sum(1 for g in generated if g.kind == "unit"),
                "functional": sum(1 for g in generated if g.kind == "functional")},
        model=settings.llm.model, endpoint=settings.llm.base_url,
    )

    result: dict[str, Any] = {
        "repo": str(repo),
        "out_dir": str(out),
        "generated": [g.to_dict() for g in generated],
        # Successes, not attempts. These counted every file the generator tried,
        # including the ones where the model returned nothing usable and no test was
        # written — so a run that produced zero files could still report "12 unit tests".
        "unit_count": sum(1 for g in generated if g.kind == "unit" and g.ok),
        "functional_count": sum(1 for g in generated
                                if g.kind == "functional" and g.ok),
        "attempted": len(generated),
        "failed": sum(1 for g in generated if not g.ok),
        "partial_count": sum(1 for g in generated if g.partial),
        # Present only when --verify ran. Absent means "not checked", which must
        # never be readable as "checked and clean".
        **({"verification": verification,
            "verdicts": {v: sum(1 for r in verification if r.get("verdict") == v)
                         for v in sorted({r.get("verdict", "") for r in verification})}}
           if verify else {}),
        "partial": {g.target: f"generated from the first {g.saw_chars} of "
                              f"{g.total_chars} chars"
                    for g in generated if g.partial},
        "recommendations": recommendations,
    }
    if coverage_ok:
        result["coverage"] = coverage_to_dict(domains, include_why=False)
    if provenance is not None:
        # Unlike coverage's `why`, `provenance` is five small keys — no
        # context-flooding reason to withhold it from stdout.
        result["provenance"] = provenance.to_dict()
    return result


def _framework_for(
    cache: dict[str, tuple[str, bool]], repo: Path, language: str, cfg
) -> tuple[str, bool]:
    """Memoized `detect_framework`, keyed by language — the repo tree is walked once
    per language per run, not once per file."""
    if language not in cache:
        default = cfg.frameworks.get(language, "the language's standard framework")
        cache[language] = detect_framework(repo, language, default)
    return cache[language]


def _gen_unit(
    repo, out, store, summaries, cfg, llm, framework_cache
) -> tuple[list[GeneratedTest], list[str], bool]:
    """Returns ``(results, skipped_paths, used_llm_summary)``.

    ``skipped_paths`` is the tail of the ranked, eligible candidate list cut off by
    ``max_unit_files`` — returned here, alongside the results, rather than
    recomputed by the caller from a second pass over ``store.file_records()``. Two
    independent computations of the same population is exactly how these counts
    have drifted apart before.

    ``used_llm_summary`` is True iff a summary actually handed to `_ask_unit` (i.e.
    the target's read succeeded — a summary is never fetched for a target that
    failed to read) was LLM-sourced. `Provenance.model` in `generate_tests` only
    names the index-time summary model when this is true.
    """
    results: list[GeneratedTest] = []
    used_llm_summary = False
    # `is_test_path` excludes the project's OWN tests as generation targets. Writing a
    # test for a test file wastes a slot from a bounded budget, and it produced the most
    # misleading artifact in the corpus: handed `gps-parser-test.cpp` as its target, the
    # model emitted a near-verbatim copy — same 22 assertions, same structure — that
    # fails. That reads like "the model broke a correct test", and it is, but only
    # because it was asked to test one. The classifier already exists; the call map has
    # used it all along.
    code_files = [r for r in store.file_records()
                  if is_code(r.language) and r.n_symbols > 0
                  and not is_test_path(r.path)]
    # file_records() is ordered by path, so a bounded run spent its whole budget on
    # whatever sorts first. On Click that meant `examples/*` demo scripts — the library
    # itself was never reached. Same shape as the analysis triage budget going to
    # vendored headers: alphabetical order is not a priority order.
    code_files.sort(key=lambda r: path_rank(r.path))

    def _one(rec) -> GeneratedTest:
        nonlocal used_llm_summary
        # Needed before the read now, not after: an unreadable file still returns a
        # GeneratedTest (below) and that record wants a real framework, not a blank.
        # Cache is fully populated (below, before the pool starts) for every language
        # in `targets`, so this is a pure read — safe from worker threads.
        framework, detected = _framework_for(framework_cache, repo, rec.language, cfg)
        capped = _read_capped(repo / rec.path, cfg.max_file_bytes)
        if capped is None:
            # A target selected for generation (it is in `targets`) that could not be
            # read is a FAILURE, not an absence — the same ruling `scan` makes for the
            # identical situation (see agents/scan/agent.py ~line 96-103). Returning None
            # here used to drop the file from `results` entirely: not attempted, not
            # failed, not partial — it vanished, and `attempted` under-reported by one.
            return GeneratedTest("unit", rec.path, rec.language, framework, "", False,
                                 error="could not be read (not valid UTF-8, or "
                                       "unreadable) — no test was generated",
                                 framework_assumed=not detected)
        content, total = capped
        summary = summaries.get(rec.path)
        if summary is not None and summary.source == "llm":
            # Assignment, not increment: idempotent, so a race between worker
            # threads (this runs inside a ThreadPoolExecutor) can only ever set it
            # to the same True — nothing to serialize.
            used_llm_summary = True
        code, cause = _ask_unit(llm, rec.path, rec.language, framework, summary, content,
                                timeout=cfg.per_file_timeout_s, temperature=_temp(cfg))
        rel_out = _unit_path(rec.path, rec.language)
        # Judge emptiness on the MODEL's output, before any banner is attached. Prepending
        # the truncation warning first made an empty generation look like a successful
        # one: the file was non-empty, so `ok` was true and the manifest reported a test
        # that contained nothing but a warning.
        produced = bool(code.strip())
        if produced and len(content) < total:
            code = _truncation_banner(rec.language, rec.path, len(content), total) + code
        ok = _write(out / rel_out, code) if produced else False
        # Name WHICH failure. A deadline and a model that looped and wrote nothing
        # are different problems with different fixes; reporting both as "no usable
        # test content" made a temperature effect unmeasurable.
        error = "" if ok else (cause or "the model returned no usable test content")
        return GeneratedTest("unit", rec.path, rec.language, framework,
                             rel_out.as_posix(), ok, error=error,
                             saw_chars=len(content), total_chars=total,
                             framework_assumed=not detected)

    targets = code_files[: cfg.max_unit_files]
    skipped = [r.path for r in code_files[cfg.max_unit_files :]]
    total_n = len(targets)
    # Populated serially, BEFORE the pool starts: every worker thread below only reads
    # `framework_cache`, never writes it, so there is no race to reason about.
    for lang in {r.language for r in targets}:
        _framework_for(framework_cache, repo, lang, cfg)
    with ThreadPoolExecutor(max_workers=max(1, cfg.workers)) as pool:
        futures = {pool.submit(_one, rec): rec for rec in targets}
        for done, fut in enumerate(as_completed(futures), start=1):
            rec = futures[fut]
            try:
                gen = fut.result()
            except Exception as exc:  # noqa: BLE001 - one file must not kill the run
                log.warning("testgen: [%d/%d] %s — FAILED: %s: %s", done, total_n,
                            rec.path, exc.__class__.__name__, exc)
                gen = GeneratedTest("unit", rec.path, rec.language, "", "", False,
                                    error=f"{exc.__class__.__name__}: {exc}")
            log.info("testgen: [%d/%d] %s — %s", done, total_n, rec.path,
                     "written" if gen.ok else "no usable output")
            results.append(gen)
    return results, skipped, used_llm_summary


def _gen_functional(
    repo, out, store, summaries, cfg, llm, framework_cache
) -> tuple[list[GeneratedTest], list[str], bool]:
    """Returns ``(results, skipped_components, used_llm_summary)`` — see `_gen_unit`
    for why the skipped population travels with the results instead of being
    recomputed, and for what ``used_llm_summary`` means."""
    results: list[GeneratedTest] = []
    used_llm_summary = False
    components = [c for c in store.load_components() if is_code(c.language)]
    edges = store.load_io_edges()
    targets = components[: cfg.max_functional_components]
    skipped = [c.name for c in components[cfg.max_functional_components :]]
    for comp in targets:
        comp_edges = [e for e in edges if e.src == comp.name or e.dst == comp.name]
        io_text = summarize_io(comp_edges) if comp_edges else "(no detected IO edges)"
        member_summaries = [summaries[f] for f in comp.files if f in summaries]
        if any(s.source == "llm" for s in member_summaries):
            used_llm_summary = True
        member_purposes = "\n".join(
            f"- {f}: {summaries[f].purpose}" for f in comp.files if f in summaries
        )[:2000]
        # Not threaded (unlike `_gen_unit`), so no race concern populating the shared
        # cache here — but it's still the same one, shared with the unit pass, so a
        # language detected once is never re-walked.
        framework, detected = _framework_for(framework_cache, repo, comp.language, cfg)
        ext = _EXT.get(comp.language, "txt")
        rel_out = Path("functional") / f"test_{_safe(comp.name)}.{ext}"
        # How much real material this component actually supplies. The functional prompt
        # carries no source — only the IO map and the member file purposes — so when a
        # component has neither, the model is handed a name and a language and nothing
        # else. It does not refuse; it invents. On the PX4 GNSS drivers that produced a
        # `test_root.cpp` importing `GpsParser.h` and asserting on `ParseResult` and
        # `ErrorCode::EMPTY_INPUT`, none of which exist anywhere in the repository — and
        # the model said so in its own first comment: "Since the implementation is not
        # provided, we define the expected interface". It was recorded `ok: true`.
        #
        # Generating from nothing is not a partial success. This is the same counting bug
        # already fixed for unit files — `ok` must never be true on a zero-length input —
        # surviving in the sibling path.
        context = member_purposes.strip()
        context_chars = len(context) + (len(io_text) if comp_edges else 0)
        if context_chars == 0:
            results.append(GeneratedTest(
                "functional", comp.name, comp.language, framework, rel_out.as_posix(),
                ok=False, saw_chars=0, total_chars=0,
                error="no input: this component has no indexed file purposes and no IO "
                      "edges, so a functional test could only be invented",
                framework_assumed=not detected))
            continue
        code, cause = _ask_functional(llm, comp.name, comp.language, framework, io_text,
                                      member_purposes, temperature=_temp(cfg))
        ok = _write(out / rel_out, code)
        # `_gen_unit`'s unreadable/empty-output paths have named their `error` since
        # the exclusivity fix; this path never did, so a `components` coverage domain
        # whose `why` said "failed" carried no cause — the same empty-answer failure
        # the `error` field exists to prevent.
        # Name WHICH failure. A deadline and a model that looped and wrote nothing
        # are different problems with different fixes; reporting both as "no usable
        # test content" made a temperature effect unmeasurable.
        error = "" if ok else (cause or "the model returned no usable test content")
        # Recorded so `saw_chars: 0` can never sit beside `ok: true` in the manifest.
        # This is context (IO map + member purposes), not source — the functional path
        # is never given source — but it is what the model actually had to work from.
        results.append(GeneratedTest("functional", comp.name, comp.language, framework,
                                     rel_out.as_posix(), ok,
                                     saw_chars=context_chars, total_chars=context_chars,
                                     error=error, framework_assumed=not detected))
    return results, skipped, used_llm_summary


def _framework_rules(framework: str) -> str:
    """Extra instruction for frameworks whose NAME is not enough to write against.

    "GoogleTest" tells a model everything it needs. "plain assert()/main()" does not — it
    is the absence of a framework, and without saying what that means the model reaches
    for the framework it knows. Spelling it out is what turns a detected convention into
    generated code that matches it.
    """
    if framework != ASSERT_MAIN:
        return ""
    return (
        " This project uses NO test framework. Write a self-contained program: include "
        "<cassert>, write one `void test_xxx()` function per case using plain `assert(...)`, "
        "and a `int main()` that calls each of them and returns 0. Do NOT include or "
        "reference gtest, gmock, Catch or any other framework — nothing in this project "
        "can build them."
    )


def _ask_unit(llm, rel, language, framework, summary, content, timeout=None,
              temperature=None) -> tuple[str, str]:
    sys = harden_system_prompt(
        f"You are an expert test engineer. Write a COMPLETE, runnable {framework} test "
        f"file in {language} for the given source file. Cover the public functions and "
        "classes, including edge cases and error paths. Use mocks/stubs for external "
        "dependencies. Output ONLY the test file content — no prose, no explanations."
        + _framework_rules(framework)
    )
    purpose = summary.purpose if summary else ""
    symbols = "; ".join(summary.key_symbols[:12]) if summary else ""
    user = (
        f"Target file: {rel} ({language})\nPurpose: {purpose}\nKey symbols: {symbols}\n\n"
        f"Source:\n{wrap_untrusted(content, 'source')}\n\nTest file:"
    )
    return _chat_code(llm, sys, user, timeout=timeout, temperature=temperature)


def _ask_functional(llm, name, language, framework, io_text, member_purposes,
                    temperature=None) -> tuple[str, str]:
    sys = harden_system_prompt(
        f"You are an expert in functional/integration testing. Write a COMPLETE "
        f"{framework} test in {language} that exercises the component's external "
        "inputs and outputs (HTTP endpoints it exposes, calls it makes, data it reads/"
        "writes) and asserts behavior. Mock external services/datastores. Output ONLY "
        "the test file content."
        + _framework_rules(framework)
    )
    user = (
        f"Component: {name} ({language})\n\n"
        f"IO map for this component:\n{wrap_untrusted(io_text, 'io-map')}\n\n"
        f"Files and their purposes:\n{wrap_untrusted(member_purposes or '(none)', 'summaries')}"
        "\n\nFunctional test file:"
    )
    return _chat_code(llm, sys, user, temperature=temperature)


def _chat_code(llm: LLMClient, sys: str, user: str, timeout: float | None = None,
               temperature: float | None = None) -> tuple[str, str]:
    """Returns ``(code, cause)``. ``cause`` is "" on success.

    The cause is carried out rather than logged and dropped because a transport failure,
    a deadline and "the model wrote nothing" are three different problems with three
    different fixes, and the manifest reported all of them as "the model returned no
    usable test content". Measuring whether a temperature change fixed degenerate
    repetition is impossible if a call that now succeeds but runs long is counted
    identically to one that looped and produced nothing.
    """
    try:
        resp = llm.chat(
            [{"role": "system", "content": sys}, {"role": "user", "content": user}],
            # No 1500-token cap. A reasoning model spends 13-15k tokens before emitting a
            # character, so every generation burned that attempt, retried, and an
            # evaluation run watched all six of its files fail this way.
            timeout=timeout,
            temperature=temperature,
        )
    except Exception as exc:  # noqa: BLE001 - one file must not kill the run
        # Returning "" silently made a transport failure indistinguishable from "the
        # model had nothing to write".
        log.warning("testgen: generation call failed: %s: %s",
                    exc.__class__.__name__, exc)
        text = str(exc)
        cause = ("timed out" if "deadline exceeded" in text or "timed out" in text
                 else f"{exc.__class__.__name__}: {text[:160]}")
        return "", cause
    code = _extract_code(resp.content)
    return code, "" if code.strip() else "empty content from the model"


# -- helpers ----------------------------------------------------------------
_OPEN_FENCE_RE = re.compile(r"^\s*```[a-zA-Z0-9_+-]*[ \t]*\n")


def _extract_code(text: str) -> str:
    """Strip the model's markdown fence, INCLUDING when it never closed one.

    The regex requires a matching closing fence. When the model runs out of budget
    mid-file there isn't one, so the match failed and the fallback returned the text
    unchanged — with the opening ```cpp still on line 1. The file was written, counted as
    a success, and only failed later at `error: expected unqualified-id` on line 1:1,
    which reads as a model-quality problem rather than an extraction bug.

    Measured: 1 of the 4 files written in the post-convention corpus run was lost to
    exactly this. Same family as the sandbox piping its exit status into `tail` and
    `-o /elsewhere` silently skipping verification — a failure invisible at the point it
    happens, surfacing somewhere it gets misattributed.
    """
    m = re.search(r"```[a-zA-Z0-9_+-]*\n(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1).strip() + "\n"
    # No closing fence. If the output opens with one, it is still a fence.
    stripped = _OPEN_FENCE_RE.sub("", text, count=1)
    return stripped.strip() + "\n" if stripped.strip() else ""


def _unit_path(rel: str, language: str) -> Path:
    ext = _EXT.get(language, "txt")
    parts = rel.split("/")
    dirs = parts[:-1]
    stem = parts[-1].rsplit(".", 1)[0]
    if language == "Go":
        name = f"{stem}_test.go"
    elif language == "Java":
        name = f"{stem.capitalize()}Test.java"
    else:
        name = f"test_{stem}.{ext}"
    return Path("unit", *dirs, name)


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_") or "component"


# Comment syntax per language, so the warning survives into the generated file itself —
# the manifest is easy to miss, the file being read is not.
_LINE_COMMENT = {"Python": "#", "Ruby": "#", "Shell": "#"}


def _truncation_banner(language: str, target: str, saw: int, total: int) -> str:
    mark = _LINE_COMMENT.get(language, "//")
    pct = saw * 100 // total if total else 0
    return (
        f"{mark} WARNING: generated from the first {saw} of {total} characters of "
        f"{target} ({pct}%).\n"
        f"{mark} Everything past that cut was NOT seen by the generator, so these tests\n"
        f"{mark} neither cover it nor can be trusted about it. Raise "
        f"testgen.max_file_bytes to cover the whole file.\n\n"
    )


def _write(path: Path, code: str) -> bool:
    if not code.strip():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(code, encoding="utf-8")
    return True


def _read_capped(path: Path, max_bytes: int) -> tuple[str, int] | None:
    """Return ``(visible_text, total_chars)``.

    The caller needs the second number. Generating tests from the first N characters and
    presenting them as tests *for the file* is how a suite ends up asserting against API
    the model never saw — and unlike a wrong summary, the output is runnable code that
    looks authoritative.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return text[:max_bytes], len(text)


def _domain_coverage(
    items: list[GeneratedTest], skipped: list[str], cap_reason: str
) -> Coverage:
    """Build one coverage domain from a `_gen_unit`/`_gen_functional` result pair.

    State per `GeneratedTest`, checked in this order so a unit lands in exactly one
    of them: `failed` if `not ok` (using `.error` as the reason — the field the
    empty-answer-failure fix exists to keep populated on every not-ok path); else
    `partial` if `.partial` (a truncated-but-written test); else counted as
    `succeeded` without a `why` entry, the same way `scan` omits successes.
    """
    why: dict[str, UnitState] = {}
    n_failed = n_partial = 0
    for g in items:
        if not g.ok:
            n_failed += 1
            why[g.target] = UnitState(
                "failed", g.error or "the model returned no usable test content")
        elif g.partial:
            n_partial += 1
            why[g.target] = UnitState(
                "partial",
                f"generated from the first {g.saw_chars} of {g.total_chars} chars")
    for target in skipped:
        why[target] = UnitState("skipped", cap_reason)
    attempted = len(items)
    return Coverage(
        eligible=attempted + len(skipped),
        attempted=attempted,
        succeeded=attempted - n_partial - n_failed,
        partial=n_partial,
        failed=n_failed,
        skipped=len(skipped),
        why=why,
    )


def _write_manifest(
    out: Path, repo: Path, generated, recommendations, banner: str,
    domains: dict[str, Coverage] | None, provenance: Provenance | None,
) -> None:
    manifest: dict[str, Any] = {
        "repo": str(repo),
        "recommendations": recommendations,
        "tests": [g.to_dict() for g in generated],
    }
    if domains is not None:
        manifest["coverage"] = coverage_to_dict(domains)
    if provenance is not None:
        manifest["provenance"] = provenance.to_dict()
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    lines = []
    if banner:
        lines += [f"**{banner}**", ""]
    lines += [
        "# secagent-generated tests",
        "",
        "These tests were drafted automatically by secagent (UC5) from the project's "
        "structure and IO map. **They are drafts — review and adjust before relying on "
        "them.** They live here, separate from the project's own test suite.",
        "",
        "## Recommended first",
        "",
    ]
    lines += [f"- {r}" for r in recommendations]
    partial = [g for g in generated if g.partial]
    if partial:
        # The README is what a reviewer reads before trusting the suite. A test file
        # generated from a third of its source looks exactly like one generated from all
        # of it, and the difference decides whether "no test for X" means "X is covered
        # elsewhere" or "the generator never saw X".
        lines += [
            "",
            "## ⚠️ Incomplete source coverage",
            "",
            f"{len(partial)} file(s) were truncated before generation, so the tests "
            "below cover only the part that was read — and say nothing reliable about "
            "the rest. Raise `testgen.max_file_bytes` to cover them fully.",
            "",
        ]
        lines += [f"- `{g.target}` — first {g.saw_chars} of {g.total_chars} chars"
                  for g in partial]
    lines += [
        "",
        "## Layout",
        "",
        "- `unit/` — per-file unit tests (mirrors the source tree).",
        "- `functional/` — component input/output tests derived from the IO map.",
        "- `manifest.json` — what was generated and for which targets.",
    ]
    if banner:
        lines += ["", "----", "", f"**{banner}**"]
    (out / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# Verdicts that mean "this is not a test". Kept rather than deleted — the artifact is the
# evidence — but moved somewhere its path alone says so, because a green no-op sitting
# beside real tests is how one gets committed.
_QUARANTINE = ("vacuous", "wrong")


def _temp(cfg) -> float | None:
    """`testgen.temperature`, or None to follow `llm.temperature`.

    0 means "follow the global" — the same escape hatch `scan.temperature` uses, so a
    deployment that has tuned `llm.temperature` for its own model is not overridden.
    """
    return cfg.temperature or None


def _verify_written(repo: Path, out: Path, generated: list[GeneratedTest]) -> list[dict]:
    """Run the mechanical gates over everything that was written, and quarantine the duds.

    The gates are orthogonal and each catches what the others cannot: measured on one real
    generated C++ file of 10 cases, the run gate caught the 3 that fail against correct
    code and the mutation gate caught the 3 that compile, run, pass and assert nothing.
    """
    from ...verify import verify_test

    results: list[dict] = []
    quarantine = out / "quarantine"
    for g in generated:
        if not g.ok:
            continue
        written = out / g.path
        if not written.is_file():
            continue
        # `out` may sit outside the repo (`testgen -o /elsewhere` is ordinary usage), so
        # the file is handed to the harness explicitly and placed inside its scratch copy
        # at this path. Deriving a repo-relative path instead would silently skip
        # verification for every out-of-tree run.
        outcome = verify_test(repo, g.path, test_file=written)
        record = outcome.to_dict()
        record["path"] = g.path
        if outcome.verdict in _QUARANTINE:
            target = quarantine / Path(g.path).name
            target.parent.mkdir(parents=True, exist_ok=True)
            written.replace(target)
            record["quarantined_to"] = target.relative_to(out).as_posix()
            g.ok = False
            g.error = f"{outcome.verdict}: {outcome.reason[:200]}"
        results.append(record)
    return results
