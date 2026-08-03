"""Detect which test framework a repo actually uses, per language.

`TestGenConfig.frameworks` (see `secagent.config`) is a static default map — e.g. C ->
Unity, C++ -> GoogleTest, C# -> xUnit — used to steer generated test code. On a
repository that has no such dependency anywhere, telling the model to write Unity/
GoogleTest/xUnit tests produces a suite that cannot run: nothing in the project can
build or execute it.

Detection here is cheap and read-only: it never executes anything in the target repo,
just looks for markers a real project of that framework would leave lying around
(vendored framework directories, build-file references, package/dependency manifest
entries). When nothing is found for a language, the configured default is still the
answer — but the caller must be told detection came up empty, since a wrong framework
silently assumed is how a whole generated suite becomes unrunnable.

Only languages with a cheap, defensible, file-only signal are covered here (C, C++,
Python, Rust, C#). Anything else always falls back to the configured default with
``detected=False`` — inventing a heuristic for languages with no reliable cheap signal
(JavaScript/TypeScript/Go/Java/Ruby package ecosystems have many equally-common test
runners with no single dominant marker) would be exactly the kind of unfounded guess
this module exists to avoid.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# Directories pruned from the walk: never contain first-party build/dependency config,
# and on a large repo can dominate the walk cost for no signal.
_IGNORE_DIRS = {
    ".git", ".svn", ".hg", "node_modules", ".venv", "venv", "__pycache__",
    "build", "dist", "target", ".mypy_cache", ".pytest_cache", ".tox",
}

_READ_LIMIT = 200_000  # cap per build/manifest file read — these are config, not data


def _read_small(path: Path, limit: int = _READ_LIMIT) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")[:limit]
    except OSError:
        return ""


def _walk(repo: Path):
    """``os.walk`` over ``repo``, pruning noise dirs. One pass per call."""
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = [d for d in dirnames if d not in _IGNORE_DIRS]
        yield Path(dirpath), dirnames, filenames


# -- C / C++ ------------------------------------------------------------------------
#
# Evidence: a vendored gtest/gmock/Unity directory, or a CMake/Makefile reference to
# one. Both are how these C-family frameworks actually get pulled into a build — there
# is no package manager signal to lean on the way Python/C# have.

_GTEST_DIR_NAMES = {"googletest", "gtest", "gmock", "googlemock"}
_GTEST_BUILD_RE = re.compile(r"googletest|gtest|gmock|GTest::", re.IGNORECASE)
_UNITY_BUILD_RE = re.compile(r"unity\.[ch]\b|ThrowTheSwitch", re.IGNORECASE)
_MAKE_CMAKE_NAMES = {"cmakelists.txt", "makefile", "gnumakefile"}


def _detect_cpp(repo: Path) -> str | None:
    for dirpath, dirnames, filenames in _walk(repo):
        if any(d.lower() in _GTEST_DIR_NAMES for d in dirnames):
            return "GoogleTest"
        for fname in filenames:
            if fname.lower() in _MAKE_CMAKE_NAMES and _GTEST_BUILD_RE.search(
                _read_small(dirpath / fname)
            ):
                return "GoogleTest"
    # No framework anywhere. Before falling back to the configured default, check whether
    # the project's own tests show it does not use one.
    return _detect_assert_main(repo, (".cpp", ".cc", ".cxx", ".c"))


# The convention with no framework at all: a test file that includes <cassert>, defines
# `main()`, and pulls in nothing else. This is not an absence of evidence — it is
# evidence, and the strongest kind available here, because it is the project's own test
# code rather than an inference from a build file.
#
# PX4-GPSDrivers is exactly this shape: `CMakeLists.txt` builds a plain `gps-parser-test`
# executable and `gps-parser-test.cpp` is `#include <cassert>` plus `assert(...)` in
# `main()`. Emitting GoogleTest into that project produces a suite nobody can build or
# merge — true whether or not anyone is verifying the output.
ASSERT_MAIN = "plain assert()/main()"

_ASSERT_INCLUDE_RE = re.compile(r'^\s*#\s*include\s*[<"](?:cassert|assert\.h)[>"]',
                                re.MULTILINE)
_MAIN_RE = re.compile(r"^\s*(?:int|void)\s+main\s*\(", re.MULTILINE)
_ANY_FRAMEWORK_RE = re.compile(r"gtest|gmock|unity\.h|catch2?/|doctest", re.IGNORECASE)
_TEST_NAME_RE = re.compile(r"(?:^|[-_])tests?(?:[-_.]|$)|^tests?[-_]", re.IGNORECASE)


def _detect_assert_main(repo: Path, exts: tuple[str, ...]) -> str | None:
    """True when the repo's OWN tests are plain assert()/main() with no framework."""
    for dirpath, _dirnames, filenames in _walk(repo):
        for fname in filenames:
            stem, dot, ext = fname.rpartition(".")
            if not dot or f".{ext.lower()}" not in exts or not _TEST_NAME_RE.search(stem):
                continue
            text = _read_small(dirpath / fname)
            if (_ASSERT_INCLUDE_RE.search(text) and _MAIN_RE.search(text)
                    and not _ANY_FRAMEWORK_RE.search(text)):
                return ASSERT_MAIN
    return None


def _detect_c(repo: Path) -> str | None:
    for dirpath, _dirnames, filenames in _walk(repo):
        for fname in filenames:
            low = fname.lower()
            if low in {"unity.c", "unity.h"}:
                return "Unity"
            if low in _MAKE_CMAKE_NAMES and _UNITY_BUILD_RE.search(
                _read_small(dirpath / fname)
            ):
                return "Unity"
    return _detect_assert_main(repo, (".c",))


# -- Python ---------------------------------------------------------------------------
#
# Evidence: `pytest` named in pyproject.toml/setup.cfg/requirements*.txt (as a
# dependency or via a `[tool.pytest.ini_options]`/`[pytest]` section), or a
# `conftest.py` — which is meaningless to anything but pytest.

_PYTEST_RE = re.compile(r"\bpytest\b", re.IGNORECASE)


def _detect_python(repo: Path) -> str | None:
    if next(repo.rglob("conftest.py"), None) is not None:
        return "pytest"
    for dirpath, _dirnames, filenames in _walk(repo):
        for fname in filenames:
            low = fname.lower()
            is_candidate = low in {"pyproject.toml", "setup.cfg"} or (
                low.startswith("requirements") and low.endswith(".txt")
            )
            if is_candidate and _PYTEST_RE.search(_read_small(dirpath / fname)):
                return "pytest"
    return None


# -- Rust -------------------------------------------------------------------------
#
# `cargo test` is not a third-party dependency to detect — it ships with the Rust
# toolchain itself and runs `#[test]` functions with zero extra setup. A `Cargo.toml`
# anywhere is the whole signal: it is not an assumption, it is a fact about the repo.

def _detect_rust(repo: Path) -> str | None:
    if next(repo.rglob("Cargo.toml"), None) is not None:
        return "cargo test"
    return None


# -- C# ---------------------------------------------------------------------------
#
# Evidence: an xUnit/NUnit/MSTest `<PackageReference>` in a `.csproj` — the standard,
# unambiguous way a .NET project declares its test framework dependency.

_PACKAGE_REF_RE = re.compile(r'<PackageReference\s+Include="([^"]+)"', re.IGNORECASE)


def _detect_csharp(repo: Path) -> str | None:
    for csproj in repo.rglob("*.csproj"):
        pkgs = {m.group(1).lower() for m in _PACKAGE_REF_RE.finditer(_read_small(csproj))}
        if any(p.startswith("xunit") for p in pkgs):
            return "xUnit"
        if any(p.startswith("nunit") for p in pkgs):
            return "NUnit"
        if any(p.startswith("mstest") for p in pkgs):
            return "MSTest"
    return None


_DETECTORS = {
    "C": _detect_c,
    "C++": _detect_cpp,
    "Python": _detect_python,
    "Rust": _detect_rust,
    "C#": _detect_csharp,
}


def detect_framework(repo: Path, language: str, default: str) -> tuple[str, bool]:
    """Return ``(framework, detected)``.

    ``detected`` is True only when real repo evidence was found — callers may present
    that as observed fact. False means ``default`` (the configured/fallback value) was
    used with no supporting evidence and callers MUST disclose that it was assumed,
    not observed.
    """
    detector = _DETECTORS.get(language)
    if detector is None:
        return default, False
    found = detector(repo)
    if found:
        return found, True
    return default, False
