"""Compile it, run it, break the code underneath it, and see whether it notices.

Three gates, and they are orthogonal — each catches what the others cannot. Measured on
one real generated C++ file of 10 test cases:

    compile   catches nothing here (all 10 built)
    run       catches the 3 that FAIL against correct code — false CI failures
    mutation  catches the 3 VACUOUS ones, which compile, run and pass while asserting
              nothing, because every assertion sits inside a branch that never executes

`affordance verify` is a fourth, answering a different question again — whether the names
a test cites exist. It caught the fabricated `GpsParser.h` that all three gates here would
have waved through. None of the four supersedes another.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
from pathlib import Path

from ..affordances.call_map import is_test_path
from ..affordances.store import AffordanceStore
from .diagnose import ENVIRONMENT, TOOLCHAIN, classify_build_failure
from .models import TestOutcome, verdict_for
from .mutation import Mutant, apply_mutant, count_assertions, mutants_for, supported
from .sandbox import DockerSandbox, Sandbox

log = logging.getLogger(__name__)

_LANG_BY_EXT = {
    ".py": "Python", ".c": "C", ".cpp": "C++", ".cc": "C++", ".cxx": "C++",
}
_SOURCE_EXT = {"C": (".c",), "C++": (".cpp", ".cc", ".cxx", ".c"), "Python": (".py",)}


def _scratch_root() -> Path:
    """Where the scratch copy lives, so a container runtime can actually mount it."""
    override = os.environ.get("SECAGENT_VERIFY_SCRATCH")
    root = Path(override) if override else Path.home() / ".cache" / "secagent-verify"
    root.mkdir(parents=True, exist_ok=True)
    return root


def language_of(path: str | Path) -> str:
    return _LANG_BY_EXT.get(Path(path).suffix.lower(), "")


def _dependency_closure(repo: Path, test_rel: str, language: str) -> list[str]:
    """Implementation files the test transitively depends on, via quoted includes.

    Both the link set and the mutation target set. Two earlier strategies were wrong in
    opposite directions, and the real PX4 repo falsified each:

    * **Everything in the repo** does not build. Linking all eleven GNSS drivers drags in
      `definitions.h`, a header PX4-Autopilot supplies from outside this repository, so
      the whole thing failed to compile and a perfectly good test was reported
      `uncompilable`.
    * **One hop** does not link. `unicore.cpp` calls `calculateCRC32` in `crc.cpp`, so
      stopping at the test's own includes leaves an undefined symbol.

    The closure lands exactly on the set this project's own `CMakeLists.txt` names —
    `gps-parser-test.cpp`, `src/unicore.cpp`, `src/crc.cpp` — without reading it, which
    matters because most repos will not have a target we can parse.

    Test files are excluded throughout: each carries its own `main()` and they collide at
    link time. `is_test_path` is the same classifier the call map uses.
    """
    exts = _SOURCE_EXT[language]
    by_stem: dict[str, str] = {}
    for p in sorted(repo.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in exts:
            continue
        rel = p.relative_to(repo).as_posix()
        if rel == test_rel or is_test_path(rel) or "secagent-tests" in rel:
            continue
        by_stem.setdefault(p.stem, rel)

    seen: set[str] = set()
    frontier = [test_rel]
    while frontier:
        current = frontier.pop()
        path = repo / current
        if not path.is_file():
            continue
        for inc in _C_INCLUDE_RE.findall(path.read_text(encoding="utf-8", errors="replace")):
            impl = by_stem.get(Path(inc).stem)
            if impl and impl not in seen:
                seen.add(impl)
                frontier.append(impl)
            header = (repo / current).parent / inc
            if header.is_file():
                frontier.append(header.relative_to(repo).as_posix())
    return sorted(seen)


_C_INCLUDE_RE = re.compile(r'^\s*#\s*include\s*"([^"]+)"', re.MULTILINE)
_PY_IMPORT_RE = re.compile(r'^\s*(?:from\s+([.\w]+)\s+import|import\s+([.\w]+))',
                           re.MULTILINE)


def _claimed_sources(work: Path, test_rel: str, language: str,
                     sources: list[str]) -> list[str]:
    """The implementation files this test CLAIMS to cover — its mutation targets.

    Distinct from the link set, and the distinction is load-bearing. The first run of this
    harness mutated every source in the repo and pronounced the PX4 project's own
    `gps-parser-test.cpp` **vacuous** — because it dutifully flipped comparisons in
    `ashtech.cpp`, `emlid_reach.cpp` and `femtomes.cpp`, which that test has never heard
    of. Every one survived, for an excellent reason, and a genuinely good test scored 0.

    A test's own includes/imports are its claim about what it covers. Mutating outside
    them measures the wrong thing. When nothing resolves, the honest answer is no mutants
    and a verdict of `unverifiable` — not a score against arbitrary code.
    """
    text = (work / test_rel).read_text(encoding="utf-8", errors="replace")
    by_stem: dict[str, list[str]] = {}
    for rel in sources:
        by_stem.setdefault(Path(rel).stem, []).append(rel)

    claimed: list[str] = []
    if language == "Python":
        names = {a or b for a, b in _PY_IMPORT_RE.findall(text)}
        for name in names:
            claimed += by_stem.get(name.split(".")[-1], [])
    else:
        for inc in _C_INCLUDE_RE.findall(text):
            claimed += by_stem.get(Path(inc).stem, [])
    # Deterministic and duplicate-free; order follows the link set for reproducibility.
    return [s for s in sources if s in set(claimed)]


# Build failure gets its own exit code so it is distinguishable from a test that ran and
# failed. 91 is outside both pytest's range and the shell's signal codes.
_BUILD_FAILED = 91

# Two things this script must never do, both of which it did in its first version and both
# of which silently made every test look like it passed:
#
#   1. Build in one container and run in another. `docker run --rm` discards the
#      filesystem, so the binary written by the build step did not exist for the run step.
#   2. Pipe the program into `tail`. A pipeline's exit status is the LAST command's, so
#      `/tmp/t | tail` reports tail's success no matter what the test did. Every test
#      passed, every mutant survived, and the harness would have called the whole world
#      vacuous while appearing to work.
#
# Hence: one invocation, output to a file, status captured explicitly before anything else
# touches `$?`.
_CXX_SCRIPT = """\
set -u
if ! clang++-14 -std=c++17 -I. -Isrc -w -o /tmp/t {files} > /tmp/build.log 2>&1; then
  tail -25 /tmp/build.log; exit {build_failed}
fi
/tmp/t > /tmp/run.log 2>&1; rc=$?
tail -25 /tmp/run.log
exit $rc
"""
# `src/` on the path as well as the root: the src-layout is the dominant Python packaging
# convention, and without it a test importing the package under test fails to COLLECT —
# reported as `wrong`, which is a verdict about the harness's own sys.path rather than
# about the test. `PYTHONPATH` is set rather than installing anything, because the sandbox
# has no network and must not acquire one.
_PY_SCRIPT = """\
set -u
export PYTHONPATH=/w:/w/src${{PYTHONPATH:+:$PYTHONPATH}}
python3 -m pytest -q -p no:cacheprovider --rootdir=/w /w/{test} > /tmp/run.log 2>&1; rc=$?
tail -40 /tmp/run.log
exit $rc
"""


# Identifiers a C-family test calls. `\b(name)\s*\(` catches both call sites and the
# `extern unsigned crc32(unsigned char *b);` declarations that make a test unlinkable by
# the include path. Keywords are excluded because `if (`/`while (` are not calls.
_CALL_RE = re.compile(r"\b([A-Za-z_]\w*)\s*\(")
_NOT_CALLS = frozenset({
    "if", "for", "while", "switch", "return", "sizeof", "assert", "static_assert",
    "catch", "main", "and", "or", "not", "defined", "printf", "fprintf", "sprintf",
    "snprintf", "memcpy", "memset", "strlen", "strcmp", "free", "malloc",
})


def _symbol_sources(repo: Path, test_source: str, sources: list[str],
                    store_dir: str) -> list[str]:
    """Implementation files resolved by SYMBOL, for tests that include no headers.

    The include-based closure is blind to a test that hand-declares its subject —
    `extern uint32_t calculateCRC32(...)` with no `#include "crc.h"` — which generated C++
    does routinely. On one real corpus that was 8 of 14 files: nothing linked, nothing
    proven, and 4 of them were reported `uncompilable` as though the tests were at fault
    when relinking by hand showed one compiles and simply fails its assertion.

    The affordance index already knows which file defines every function, so this needs no
    new analysis — only a reverse lookup it already supports. The index is used when the
    repo has one and skipped silently when it does not: `verify-tests` grades arbitrary
    repos and must not require them to be indexed first.
    """
    called = {m for m in _CALL_RE.findall(test_source) if m not in _NOT_CALLS}
    if not called:
        return []
    store = AffordanceStore(repo, store_dir)
    try:
        defined_in: dict[str, str] = {}
        for name, path in store.function_symbol_names():
            # Qualified C++ names arrive as `Class::method`; match on either spelling.
            defined_in.setdefault(name, path)
            defined_in.setdefault(name.rsplit("::", 1)[-1], path)
    except Exception:  # noqa: BLE001 - an unreadable index is "no index", not a failure
        return []
    finally:
        store.close()

    by_stem = {Path(rel).stem: rel for rel in sources}
    out: list[str] = []
    for name in sorted(called):
        declared_in = defined_in.get(name)
        if declared_in is None:
            continue
        # The index points at the DECLARING file, which for C++ is often the header.
        # The implementation is its sibling in the link set.
        impl = (declared_in if declared_in in sources
                else by_stem.get(Path(declared_in).stem))
        if impl and impl not in out:
            out.append(impl)
    return out


def _build_and_run(sandbox: Sandbox, work: Path, test_rel: str, language: str,
                   sources: list[str], timeout_s: float):
    """Returns ``(compiled, ran, passed, detail)``."""
    if language == "Python":
        r = sandbox.run(work, ["sh", "-c", _PY_SCRIPT.format(test=test_rel)],
                        language, timeout_s)
        if r.timed_out:
            return None, False, False, "test timed out"
        return None, True, r.returncode == 0, (r.stdout or r.stderr)[-2000:]

    files = " ".join(f"'{s}'" for s in [test_rel, *sources])
    r = sandbox.run(
        work,
        ["sh", "-c", _CXX_SCRIPT.format(files=files, build_failed=_BUILD_FAILED)],
        language, timeout_s)
    if r.timed_out:
        return False, False, False, "build or test timed out"
    detail = (r.stdout or r.stderr)[-2000:]
    if r.returncode == _BUILD_FAILED:
        return False, False, False, detail
    return True, True, r.returncode == 0, detail


def verify_test(
    repo: Path,
    test_rel: str,
    *,
    sandbox: Sandbox | None = None,
    max_mutants: int = 10,
    timeout_s: float = 120.0,
    test_file: Path | None = None,
    store_dir: str = ".secagent",
) -> TestOutcome:
    """Run all three gates over one test file and return its verdict.

    ``test_file`` supplies the test from OUTSIDE the repository, placed at ``test_rel``
    inside the scratch copy. `testgen -o /somewhere/else` is ordinary usage, and without
    this the harness could not see its own output: the path would not be repo-relative and
    verification would silently no-op, which is the failure mode this whole module exists
    to prevent.
    """
    sandbox = sandbox or DockerSandbox()
    language = language_of(test_rel)
    test_path = repo / test_rel
    outcome = TestOutcome(path=test_rel, language=language or "unknown",
                          verdict="unverifiable")

    if not language or not supported(language):
        outcome.reason = f"no verification support for {language or Path(test_rel).suffix}"
        return outcome
    if not sandbox.available(language):
        # Refusing to check is a fine outcome. Checking by running hostile code on the
        # host is not, so there is no fallback path here on purpose.
        outcome.reason = ("no sandbox available for this language — the gates are not "
                          "run outside a container")
        return outcome
    source_path = test_file if test_file is not None else test_path
    if not source_path.is_file():
        outcome.reason = "test file not found"
        return outcome

    source = source_path.read_text(encoding="utf-8", errors="replace")
    outcome.assertions_n = count_assertions(source)

    # One scratch COPY. The caller's repo is never mounted into the sandbox and never
    # mutated — mutation rewrites implementation files, and doing that in place would be
    # editing the user's source to run a test.
    #
    # NOT under the system temp directory, and this is not a preference. On macOS
    # `tempfile` returns `/var/folders/...`, which Docker Desktop and Colima do not share
    # into the VM: the bind mount silently succeeds and arrives EMPTY, so every build
    # failed with "no such file or directory" for files that plainly existed on the host.
    # `$SECAGENT_VERIFY_SCRATCH` overrides it for hosts that share somewhere else.
    with tempfile.TemporaryDirectory(prefix="secagent-verify-",
                                     dir=_scratch_root()) as td:
        work = Path(td) / "repo"
        shutil.copytree(repo, work, ignore=shutil.ignore_patterns(
            ".git", ".secagent", "__pycache__", "*.o", "build"))
        if test_file is not None:
            placed = work / test_rel
            placed.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(test_file, placed)
        sources = _dependency_closure(work, test_rel, language) if language != "Python" else []
        if language != "Python" and not sources:
            # The test includes nothing. Fall back to resolving what it CALLS against the
            # affordance index, then close over that set's own includes so its
            # dependencies come too.
            all_impl = [p.relative_to(work).as_posix() for p in sorted(work.rglob("*"))
                        if p.is_file() and p.suffix.lower() in _SOURCE_EXT[language]
                        and not is_test_path(p.relative_to(work).as_posix())
                        and "secagent-tests" not in p.relative_to(work).as_posix()]
            seeds = _symbol_sources(repo, source, all_impl, store_dir)
            resolved = list(seeds)
            for seed in seeds:
                for extra in _dependency_closure(work, seed, language):
                    if extra not in resolved:
                        resolved.append(extra)
            sources = sorted(resolved)
        if language != "Python" and not sources:
            # Nothing to link against, so a build failure here says nothing about the
            # test. The closure is INCLUDE-based, and a test that hand-declares
            # `extern uint32_t calculateCRC32(...)` instead of including "crc.h" gives it
            # nothing to follow — 4 of 5 `uncompilable` verdicts in one corpus run were
            # this, misreported as test defects when relinking by hand showed one of them
            # compiles fine and simply fails its assertion. Refusing to judge is right;
            # judging on a link line we failed to construct is not.
            outcome.reason = (
                "no implementation could be resolved: this test declares its subject with "
                "`extern` rather than including a header, and the dependency closure "
                "follows quoted includes. Nothing was linked, so nothing is proven.")
            return outcome

        compiled, ran, passed, detail = _build_and_run(
            sandbox, work, test_rel, language, sources, timeout_s)
        outcome.compiled, outcome.ran, outcome.passed_on_correct_code = compiled, ran, passed
        if not (ran and passed):
            outcome.verdict = verdict_for(compiled=compiled, ran=ran, passed=passed,
                                          mutants_total=0, mutants_killed=0)
            outcome.reason = detail.strip()[:1000] or "no output"
            if compiled is False:
                outcome.failure_cause = classify_build_failure(work, test_rel, detail)
                if outcome.failure_cause == TOOLCHAIN:
                    # A framework this host does not have. Coverage, not a pass, and not
                    # a judgement about the test.
                    outcome.verdict = "unverifiable"
                elif outcome.failure_cause == ENVIRONMENT:
                    # The repo itself expects this header from outside the checkout. That
                    # is not a defect in the test, and calling it one would blame the
                    # generator for a missing dependency.
                    outcome.verdict = ENVIRONMENT
            return outcome

        # Gate 3. Only now — a mutation score against a failing baseline is noise.
        # Targets are what the test CLAIMS to cover, not everything that was linked.
        if language == "Python":
            all_sources = [p.relative_to(work).as_posix() for p in sorted(work.rglob("*.py"))
                           if not is_test_path(p.relative_to(work).as_posix())]
            targets = _claimed_sources(work, test_rel, language, all_sources)
        else:
            # The closure is already exactly what the test exercises transitively.
            targets = sources
        candidates: list[Mutant] = []
        for rel in targets:
            if rel == test_rel:
                continue
            text = (work / rel).read_text(encoding="utf-8", errors="replace")
            candidates += mutants_for(rel, text, language, limit=max_mutants)

        step = max(1, len(candidates) // max_mutants) if candidates else 1
        for mutant in candidates[::step][:max_mutants]:
            target = work / mutant.path
            original = target.read_text(encoding="utf-8", errors="replace")
            try:
                target.write_text(apply_mutant(original, mutant))
            except ValueError:
                continue
            try:
                m_compiled, m_ran, m_passed, _ = _build_and_run(
                    sandbox, work, test_rel, language, sources, timeout_s)
            finally:
                target.write_text(original)
            if m_compiled is False or not m_ran:
                # A mutant that will not build proves nothing about the test. Excluded
                # from the denominator rather than counted as survived, which would make
                # a good test look weak.
                outcome.mutants_invalid += 1
                continue
            outcome.mutants_total += 1
            if m_passed:
                outcome.survived.append(mutant.label)
            else:
                outcome.mutants_killed += 1

    outcome.verdict = verdict_for(
        compiled=outcome.compiled, ran=outcome.ran,
        passed=outcome.passed_on_correct_code,
        mutants_total=outcome.mutants_total, mutants_killed=outcome.mutants_killed)
    if outcome.verdict == "vacuous":
        outcome.reason = (
            f"passes against correct code and against all {outcome.mutants_total} "
            "mutants — it asserts nothing about this implementation")
    elif outcome.verdict == "unverifiable" and not outcome.reason:
        outcome.reason = (
            "nothing to mutate: no implementation file matched this test's own "
            "includes/imports, so there was no claim to check it against"
            if not targets else
            "no valid mutants could be built, so nothing was proven")
    return outcome
