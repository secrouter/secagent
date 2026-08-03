"""One ranking for every bounded selection.

Any command that stops after `max_*` items must decide which items. Four commands
independently decided "whichever sorts first", and all four were wrong in the same way:

- analysis triage spent its entire budget on vendored ``cFS/osal/`` headers, because they
  sort before ``fsw/src/`` — the application's own buffer-overflow findings were never
  triaged in any run;
- ``testgen`` spent its whole budget on ``examples/*`` and never reached Click's library;
- the docs describe pass did the same, sending 120 function descriptions to demo scripts;
- ``scan`` inherited the same shape from its walk order.

Each was fixed separately, with its own segment list and its own subtly different rules.
Alphabetical order is not a priority order, and the fifth command to need this should not
have to rediscover that.

The ranking is deliberately coarse. It answers one question — *is this the user's own
production code?* — because that is the question every one of those bugs got wrong. It is
not a relevance score; `retrieval.py` does relevance.
"""

from __future__ import annotations

import re

# Code that belongs to somebody else. A budget spent explaining a warning in a vendored
# SDK header is a budget not spent on code the user can change.
VENDOR_SEGMENTS = frozenset({
    # cFS ships its framework as three sibling directories — `cfe` (Core Flight
    # Executive), `osal`, `psp` — beside the user's own `apps/`. Only two were listed, so
    # `cFS/cfe/...` tied with app code at rank 0 and the alphabetical tiebreak put it
    # first ("c" < "f"): four of ten triage slots on `fm_dispatch.c` went to `cfe_sb.h`.
    # Checked against a real cFS tree rather than added one report at a time — the path
    # segments appearing across every finding in the cFS IKOS corpus are `apps`, `fm`,
    # `fsw`, `src`, `inc`, `cfe`, `modules`, `core_api`, `osal`, `os`, and `cfe` was the
    # only framework one missing. `libs`, `tools` and `apps` are deliberately NOT added:
    # too generic to claim as vendored in an arbitrary repo, and `tools` is already a
    # demo segment.
    "cfe", "osal", "psp", "vendor", "vendored", "third_party", "thirdparty", "external",
    "extern", "node_modules", "site-packages", ".venv", "venv", "_deps", "deps",
    "build", "dist", "target", "obj", "bin",
})

# The user's own code, but written to demonstrate or exercise the thing rather than to be
# the thing. Real, worth documenting eventually, never worth spending a bounded budget on
# before the library itself.
DEMO_SEGMENTS = frozenset({
    "examples", "example", "samples", "sample", "demo", "demos",
    "benches", "benchmarks", "bench", "fuzz",
    "tests", "test", "testing", "spec", "specs",
    "docs", "doc", "scripts", "tools",
})


def is_vendored(path: str) -> bool:
    """True when any directory in ``path`` marks it as somebody else's code."""
    return bool(VENDOR_SEGMENTS.intersection(_dirs(path)))


# Test/demo-shaped filenames, applied ONLY to files with no directory at all. Hyphen and
# underscore both, because `gps-parser-test.cpp` evaded a regex that required underscore.
_ROOT_DEMO_NAME_RE = re.compile(
    r"(?:^|[-_])tests?(?:[-_.]|$)"      # gps-parser-test.cpp, run_tests.sh, test.c
    r"|^tests?[-_]"                     # test-harness.cpp
    r"|(?:^|[-_])(?:demo|example|sample|bench|benchmark|fuzz)s?(?:[-_.]|$)",
    re.IGNORECASE,
)


def is_demo(path: str) -> bool:
    """True when ``path`` is an example, benchmark, test or script rather than library.

    Directories decide this wherever there are any: ``src/test_helpers.c`` is library code
    that happens to be named for tests, and demoting it on its filename would be wrong.

    A file at the REPO ROOT has no directory to vouch for it, and that is the case this
    used to miss entirely. `gps-parser-test.cpp` sat at the root of the PX4 GPS drivers,
    was classified as library code, sorted alphabetically ahead of every `src/...` path,
    and so was the first file processed by the function-description budget — spending 11
    of a 120-function cap on `test_empty`/`test_garbage` before any library code was
    described. `UnicoreParser`'s nine methods were last in the queue and got nothing, so
    the generated docs describe the tests OF `UnicoreParser` in detail and say nothing
    about `UnicoreParser` itself.

    So: name-matching applies at the root and nowhere else. Both cases are pinned in
    tests/test_priority.py.
    """
    dirs = _dirs(path)
    if dirs:
        return bool(DEMO_SEGMENTS.intersection(dirs))
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    return bool(_ROOT_DEMO_NAME_RE.search(name.rsplit(".", 1)[0]))


def path_rank(path: str) -> tuple[bool, bool, str]:
    """Sort key: the user's own library code first, then demos, then vendored code.

    The trailing path keeps ordering deterministic, so two runs over the same repo select
    the same items — otherwise a bounded result could not be compared with itself.
    """
    return (is_vendored(path), is_demo(path), path)


def _dirs(path: str) -> set[str]:
    """Directory segments of ``path``, excluding the filename.

    The filename is excluded on purpose: ``src/test_helpers.c`` is library code that
    happens to be named for tests, while ``tests/helpers.c`` is not.
    """
    return set(path.replace("\\", "/").split("/")[:-1])
