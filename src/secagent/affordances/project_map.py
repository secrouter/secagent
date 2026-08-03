"""Project structure mapping.

Produces a compact, pruned view of the repository: language mix, components,
entrypoints, build files, and a directory outline. Built from the file records the
store has already collected, so it does no IO of its own.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from .call_map import is_test_path
from .io_map import build_components
from .languages import BUILD_FILES, is_code
from .models import FileRecord, ProjectMap

# Filenames / patterns that usually mark an executable entrypoint.
_ENTRYPOINT_NAMES = {
    "main.py", "__main__.py", "app.py", "wsgi.py", "asgi.py", "manage.py",
    "cli.py", "index.js", "server.js", "main.go", "main.rs", "Main.java",
    # .NET 6+ top-level statements (the default template since `dotnet new`'s 2021
    # refresh) compile straight into an implicit Main with no `Main` symbol at all, so
    # the symbol-regex path below can't see them — measured on eShop, whose three real
    # entrypoints (`src/Web/Program.cs`, `src/PublicApi/Program.cs`,
    # `src/BlazorAdmin/Program.cs`) all use top-level statements and produced ZERO
    # `Main` occurrences. `Program.cs` is the .NET filename convention for the
    # entrypoint regardless of which style a given project uses, the same way
    # `main.py`/`index.js` are conventions above rather than parses.
    "Program.cs",
}

# Function names that mark an executable / framework entrypoint: a bare ``main`` or the
# ``*Main`` convention (CamelCase, so ``domain``/``remain`` don't match). Covers C/Go/Rust
# ``main``, Java ``main``, Windows ``WinMain``, and framework entrypoints whose name is a
# function rather than a file — notably NASA cFS apps' ``FM_AppMain`` / ``CFE_PSP_Main``.
_ENTRYPOINT_SYMBOL_RE = re.compile(r"^main$|Main$")


def entrypoint_files_from_symbols(symbols: Iterable[tuple[str, str]]) -> set[str]:
    """Files that define an entrypoint *function* (``main`` / ``*Main`` / ``*_AppMain``).

    Complements the filename-based detection so frameworks whose entrypoint is a function,
    not a ``main.py``/``index.js`` file, are still surfaced. ``symbols`` is an iterable of
    ``(name, path)`` pairs (e.g. from :meth:`AffordanceStore.function_symbol_names`).
    Test doubles are excluded — a unit-test stub defining ``FM_AppMain`` is not an
    entrypoint, so paths under test/stub directories don't count.
    """
    return {path for name, path in symbols
            if _ENTRYPOINT_SYMBOL_RE.search(name) and not is_test_path(path)}


def build_project_map(
    root: str,
    records: list[FileRecord],
    *,
    entrypoint_hints: set[str] | None = None,
    component_depth: int = 2,
) -> ProjectMap:
    entrypoint_hints = entrypoint_hints or set()
    languages: dict[str, int] = {}
    build_files: list[str] = []
    entrypoints: list[str] = []
    total_loc = 0

    for r in records:
        languages[r.language] = languages.get(r.language, 0) + 1
        total_loc += r.loc
        name = r.path.split("/")[-1]
        if name in BUILD_FILES:
            build_files.append(r.path)
        if name in _ENTRYPOINT_NAMES or r.path in entrypoint_hints:
            entrypoints.append(r.path)

    components = [c for c in build_components(records, component_depth) if _has_code(c.language)]

    return ProjectMap(
        root=root,
        languages=languages,
        components=components,
        entrypoints=sorted(set(entrypoints)),
        build_files=sorted(build_files),
        tree=_build_tree(records),
        file_count=len(records),
        total_loc=total_loc,
    )


def _has_code(language: str) -> bool:
    return is_code(language) or language in ("Build",)


def _build_tree(records: list[FileRecord], max_entries: int = 400) -> dict[str, Any]:
    """A nested dict outline of directories -> {files: [...], dirs: {...}}."""
    root: dict[str, Any] = {"dirs": {}, "files": []}
    for i, r in enumerate(records):
        if i >= max_entries:
            break
        parts = r.path.split("/")
        node = root
        for d in parts[:-1]:
            node = node["dirs"].setdefault(d, {"dirs": {}, "files": []})
        node["files"].append(parts[-1])
    return root
