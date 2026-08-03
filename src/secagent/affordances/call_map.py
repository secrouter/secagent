"""Build the inter-file call map from clang-extracted definitions and call sites.

Pass 2 of the analysis: given every function definition (name -> file) and every call
site (caller, callee name, file) collected by :mod:`clang_ast`, resolve each call to
the file that *defines* the callee and emit a deduplicated file->file edge. Only calls
whose callee is defined within the indexed scope become edges (so an architecture view
shows real intra-project relationships, not calls into uninstrumented libraries).
"""

from __future__ import annotations

import re

from .clang_ast import CallSite, FuncDef
from .models import CallEdge

# Directory names that mark test doubles. Matched as whole path SEGMENTS, not as
# substrings: `stub` in a substring test claims `src/stubgen.c`, and `/tests/` misses a
# repo-root `tests/` because it demands a leading slash. Both directions cost real
# accuracy — a false positive hides a real implementation from name resolution, and a
# false negative lets a test double win over it, which is the very thing this exists to
# prevent.
_TEST_DIRS = frozenset({
    "test", "tests", "unit-test", "unit_test", "unit-tests", "unit_tests",
    "testing", "mock", "mocks", "stub", "stubs", "fixtures", "testdata",
})
# Filename shapes that mark a test file wherever it lives.
_TEST_FILE_RE = re.compile(
    r"(?:^|[/_])test_[^/]*$"          # test_foo.c
    # Hyphen as well as underscore: `gps-parser-test.cpp` passed this filter, so a
    # root-level test executable was accepted as the project entrypoint and the Overview
    # page — the first and often only page a newcomer reads — framed a multi-protocol GPS
    # driver library as a GPS-parsing test program.
    r"|[-_]tests?\.[^./]+$"           # foo_test.c / foo-test.cpp / foo_tests.c
    r"|[-_]stubs?\.[^./]+$"           # foo_stubs.c
    r"|(?:^|/)conftest\.py$"
)


def is_test_path(path: str) -> bool:
    """True if the path is test/mock/stub material rather than an implementation."""
    p = path.lower()
    segments = p.split("/")
    if any(seg in _TEST_DIRS for seg in segments[:-1]):
        return True
    return bool(_TEST_FILE_RE.search(p))


def build_definition_table(functions: list[FuncDef]) -> dict[str, str]:
    """Map ``function name -> defining file``, preferring non-test definitions.

    On a name defined in both a real source file and a test stub, the real file wins so
    that calls resolve to the implementation, not the double.
    """
    table: dict[str, str] = {}
    for fn in functions:
        cur = table.get(fn.name)
        if cur is None or (is_test_path(cur) and not is_test_path(fn.file)):
            table[fn.name] = fn.file
    return table


def build_local_definitions(functions: list[FuncDef]) -> dict[str, set[str]]:
    """``file -> {names defined in it}``, for same-file-wins resolution."""
    local: dict[str, set[str]] = {}
    for fn in functions:
        local.setdefault(fn.file, set()).add(fn.name)
    return local


def resolve_calls(calls: list[CallSite], defs: dict[str, str],
                  include_intra_file: bool = False,
                  local_defs: dict[str, set[str]] | None = None) -> list[CallEdge]:
    """Turn call sites into deduplicated call edges (callee defined in-scope).

    ``local_defs`` (file -> names) makes a same-file definition win, which is required
    for C: two files may each define `static void helper(void)`.

    ``include_intra_file`` keeps edges whose caller and callee live in the SAME file.
    The file->file *map* wants only inter-file edges, but "who calls X" is a question
    about a symbol, not about files: in cFS, ``FM_ChildProcess`` dispatches to
    ``FM_ChildDeleteCmd`` a few hundred lines away in the same file, and dropping that
    made ``callers`` answer "No callers found" for a function called from a switch two
    hundred lines up. Store them; let the file->file consumers filter (see
    :func:`inter_file`).
    """
    edges: dict[tuple[str, str, str, str], CallEdge] = {}
    for cs in calls:
        # A definition in the CALLER's own file wins. C file-local `static` functions
        # legitimately share names across files, and the global name->file table keeps
        # only one of them, so whichever was indexed first captured every call to that
        # name — fabricating a cross-file dependency that does not exist.
        if local_defs is not None and cs.callee in local_defs.get(cs.file, ()):
            dst: str | None = cs.file
        else:
            dst = defs.get(cs.callee)
        if dst is None:
            continue
        if dst == cs.file and not include_intra_file:
            continue
        edge = CallEdge(cs.file, dst, cs.caller, cs.callee, line=cs.line)
        # Keep the LOWEST call-site line when the same caller/callee pair repeats, so
        # the representative site is "the first place it happens" regardless of the
        # order the parser walked the AST in. Plain assignment kept whichever was
        # visited last, which is not a property anyone can reason about.
        prev = edges.get(edge.key())
        if prev is None or (edge.line and (not prev.line or edge.line < prev.line)):
            edges[edge.key()] = edge
    return list(edges.values())


def inter_file(edges: list[CallEdge]) -> list[CallEdge]:
    """Only edges that cross a file boundary — the file->file map's view."""
    return [e for e in edges if e.src_file != e.dst_file]


def summarize_calls(edges: list[CallEdge], max_lines: int = 200) -> str:
    """A compact, agent-readable rendering of the call map (file -> file: callees)."""
    by_pair: dict[tuple[str, str], set[str]] = {}
    for e in edges:
        by_pair.setdefault((e.src_file, e.dst_file), set()).add(e.callee)
    lines = [f"# call map ({len(edges)} edges)"]
    for (src, dst), callees in sorted(by_pair.items()):
        names = ", ".join(sorted(callees)[:6])
        more = "" if len(callees) <= 6 else f" (+{len(callees) - 6})"
        lines.append(f"  {src} -> {dst}: {names}{more}")
        if len(lines) >= max_lines:
            lines.append("  ...")
            break
    return "\n".join(lines)
