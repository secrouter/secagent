"""Why a build failed, and therefore whether anything can be done about it.

The repair loop is deliberately restricted to the compile gate, and even there not to
every failure. Measured on the PX4 C++ corpus, 5 of 6 written files failed to compile for
four distinct causes, and only two of them are a model's business:

    3  missing external `definitions.h`  -> ENVIRONMENT. Supplied by PX4-Autopilot and
                                            not vendored in this checkout. The test cannot
                                            fix it and blaming the generator would be its
                                            own silent wrongness.
    3  hand-written mock GPSHelper AND   -> REPAIRABLE. Mechanical, deterministic, with an
       the real header included             unambiguous success signal.
    1  undefined fixture RTCMPingTest    -> REPAIRABLE. A plain typo.
    1  `#include "GpsParser.h"`          -> FABRICATED. The file exists nowhere; the model
                                            invented the component. Not a repair, a retry.
"""

from __future__ import annotations

import re
from pathlib import Path

ENVIRONMENT = "environment"
# A missing ANGLE-bracket include is a third-party library this host does not have, not a
# fabrication and not a repo gap. `gtest/gtest.h` is the case that matters: the generated
# tests all use GoogleTest, which is absent here and which devT had to build from source
# to check them at all. Reporting that as a bad test would be wrong twice over — the test
# may be fine, and the real finding is that the project does not use GoogleTest anyway.
TOOLCHAIN = "missing-framework"
FABRICATED = "fabricated-include"
REPAIRABLE = "repairable"

_MISSING_INCLUDE_RE = re.compile(r"'([^']+)' file not found|fatal error: ([^\s:]+): No such")


def missing_includes(build_log: str) -> list[str]:
    return [a or b for a, b in _MISSING_INCLUDE_RE.findall(build_log or "")]


_ANGLE_RE = re.compile(r'^\s*#\s*include\s*<([^>]+)>', re.MULTILINE)


def classify_build_failure(repo: Path, test_rel: str, build_log: str) -> str:
    """Environment gap, fabrication, or something a model could plausibly fix.

    The distinction between the first two is mechanical and needs no judgement: if the
    missing header is also included by the REPO's own sources, the repository expects it
    from outside and its absence is an environment gap. If only the generated test asks
    for it, the model invented it.
    """
    missing = missing_includes(build_log)
    if not missing:
        return REPAIRABLE
    test_path = repo / test_rel
    angle = set(_ANGLE_RE.findall(
        test_path.read_text(encoding="utf-8", errors="replace"))) if test_path.is_file() else set()
    for header in missing:
        if header in angle:
            # The source itself declares this external. We simply do not have it.
            return TOOLCHAIN
        stem = Path(header).name
        wanted_by_repo = False
        for p in repo.rglob("*"):
            if not p.is_file() or p.suffix.lower() not in (
                    ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".hh"):
                continue
            rel = p.relative_to(repo).as_posix()
            if rel == test_rel or "secagent-tests" in rel:
                continue
            try:
                if stem in p.read_text(encoding="utf-8", errors="replace"):
                    wanted_by_repo = True
                    break
            except OSError:
                continue
        if wanted_by_repo:
            return ENVIRONMENT
    return FABRICATED
