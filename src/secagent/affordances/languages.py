"""Language detection and file-walking utilities."""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterator
from pathlib import Path

# Extension -> language name.
EXT_LANG: dict[str, str] = {
    ".py": "Python",
    ".pyi": "Python",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".mjs": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".go": "Go",
    ".rs": "Rust",
    ".java": "Java",
    ".kt": "Kotlin",
    ".rb": "Ruby",
    ".php": "PHP",
    ".c": "C",
    ".h": "C",
    ".cpp": "C++",
    ".cc": "C++",
    ".hpp": "C++",
    ".cs": "C#",
    ".scala": "Scala",
    ".sh": "Shell",
    ".sql": "SQL",
    ".yaml": "YAML",
    ".yml": "YAML",
    ".toml": "TOML",
    ".json": "JSON",
    ".md": "Markdown",
    ".rst": "reStructuredText",
    ".proto": "Protobuf",
    ".csproj": "MSBuild",
    ".fsproj": "MSBuild",
    ".vbproj": "MSBuild",
    ".sln": "MSBuild",
    ".props": "MSBuild",
    ".targets": "MSBuild",
}

# Filenames that signal build/config/entrypoint roles.
BUILD_FILES = {
    "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "Pipfile",
    "package.json", "go.mod", "Cargo.toml", "pom.xml", "build.gradle",
    "Dockerfile", "docker-compose.yml", "docker-compose.yaml", "Makefile",
    ".gitlab-ci.yml", "Gemfile", "composer.json",
}

# Languages we treat as "code" for component/symbol purposes.
CODE_LANGS = {
    "Python", "JavaScript", "TypeScript", "Go", "Rust", "Java", "Kotlin",
    "Ruby", "PHP", "C", "C++", "C#", "Scala",
}


def detect_language(path: Path) -> str:
    if path.name in BUILD_FILES:
        return "Build"
    suffix = path.suffix.lower()
    # ``.h`` is the one extension C and C++ share, so extension alone is a guess — a
    # C++ header full of `class`/`template`/`namespace` told a downstream consumer (e.g.
    # testgen) it was C would then recommend a C test framework (Unity) for it. Sniff
    # the content for real C++ constructs and resolve to C++ when present; a genuine C
    # header (or one we can't read — some callers only have a path, not a live file)
    # keeps the plain extension answer.
    if suffix == ".h" and _looks_like_cpp_header(path):
        return "C++"
    return EXT_LANG.get(suffix, "Other")


# Read only a prefix: the constructs below appear early in real headers (after include
# guards/includes), and capping avoids pulling multi-MB vendored single-header libraries
# fully into memory just to answer a language guess.
_SNIFF_BYTES = 65536

_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT_RE = re.compile(r"//[^\n]*")
_STRING_LIT_RE = re.compile(r'"(?:\\.|[^"\\\n])*"')
_CHAR_LIT_RE = re.compile(r"'(?:\\.|[^'\\\n])*'")
# Deliberately narrow: only these three, and only as whole tokens outside comments/
# strings. Weaker signals (`struct`, `//` comments, `::`) are legal C99+ or otherwise
# too common in genuine C headers to use as evidence — a sniffer that fires on any of
# those would call plenty of real C headers C++, which is worse than the plain
# extension guess it replaces.
_CPP_TOKEN_RE = re.compile(r"\b(?:class|template|namespace)\b")


def _looks_like_cpp_header(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            data = fh.read(_SNIFF_BYTES)
    except OSError:
        # No readable file (path doesn't exist, isn't a real file, or a permissions
        # issue) — fall back to the extension answer rather than crash.
        return False
    text = data.decode("utf-8", errors="ignore")
    # Strip comments and string/char literals first so `// class Foo` or
    # `char *msg = "class-based";` cannot masquerade as a real declaration.
    text = _BLOCK_COMMENT_RE.sub(" ", text)
    text = _LINE_COMMENT_RE.sub(" ", text)
    text = _STRING_LIT_RE.sub('""', text)
    text = _CHAR_LIT_RE.sub("''", text)
    return bool(_CPP_TOKEN_RE.search(text))


def is_code(language: str) -> bool:
    return language in CODE_LANGS


# Version-control metadata, identified by reserved names (not by content), so source
# files that merely use/implement a VCS as a feature are never matched. Covers Git
# (incl. submodule `.git` pointer files) and the other common VCS dirs.
_VCS_DIRS = frozenset({".git", ".svn", ".hg", ".bzr", "_darcs", "CVS"})
# Repository-host platform config (CI workflows, issue/PR templates, CODEOWNERS, …).
# Part of the project's version-control tooling rather than its source/architecture.
_VCS_PLATFORM_DIRS = frozenset({".github", ".gitlab"})
_VCS_FILES = frozenset({
    ".git",  # submodule pointer file (`gitdir: ...`)
    ".gitignore", ".gitmodules", ".gitattributes", ".gitkeep", ".mailmap",
    ".hgignore", ".hgtags",
})


def is_vcs_metadata(rel: str) -> bool:
    """True if the repo-relative path is project version-control metadata/tooling.

    Matches, by reserved name only: a `.git`/`.svn`/… directory at any depth
    (submodules, monorepos), a submodule `.git` pointer file, a VCS dotfile, or a
    repository-host platform directory (`.github`/`.gitlab`).
    """
    parts = rel.split("/")
    for part in parts[:-1]:
        if part in _VCS_DIRS or part in _VCS_PLATFORM_DIRS:
            return True
    return parts[-1] in _VCS_DIRS or parts[-1] in _VCS_FILES


def walk_files(
    root: Path, ignore_globs: list[str], ignore_vcs: bool = True
) -> Iterator[Path]:
    """Yield files under ``root`` (recursively) that are not ignored.

    Ignore globs are matched against the repo-relative POSIX path. When ``ignore_vcs``
    is set, the project's own version-control metadata is skipped as well.
    """
    root = root.resolve()
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if ignore_vcs and is_vcs_metadata(rel):
            continue
        if _ignored(rel, ignore_globs):
            continue
        yield p


def _ignored(rel: str, ignore_globs: list[str]) -> bool:
    for pat in ignore_globs:
        # `**/node_modules/**` conventionally means "anywhere", but fnmatch's `**/` needs
        # a leading path segment, so the *root* copy (`node_modules/x` — the usual layout)
        # would slip through. Also try the pattern with the leading `**/` stripped.
        candidates = [pat]
        if pat.startswith("**/"):
            candidates.append(pat[3:])
        for p in candidates:
            if fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(rel, p.rstrip("/*")):
                return True
            # Match directory-prefix patterns like ".git/**" / "build/**".
            base = p.split("/**", 1)[0]
            if base and (rel == base or rel.startswith(base + "/")):
                return True
    return False


def rel_posix(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()
