"""C/C++ regex symbol fallback (when libclang is absent) and root-level ignore-glob
matching. Regression coverage for the extraction-layer review findings."""

from __future__ import annotations

from secagent.affordances.languages import _ignored
from secagent.affordances.symbols import extract_symbols

_C_SOURCE = """\
#include <cfe.h>
struct FM_Data { int x; };
typedef struct FM_Global_t {
  int y;
} FM_Global_t;
enum FM_State { IDLE, BUSY };

int32 FM_AppInit(uint32 x, char *y)
{
    FM_ChildInit();
    return CFE_SUCCESS;
}

void FM_AppMain(void) {
    while (running) { DoWork(); }
}

void FM_Prototype(void);
"""


def test_c_regex_fallback_extracts_defs_not_calls():
    syms = {(s.kind, s.name) for s in extract_symbols("fm.c", _C_SOURCE, "C")}
    # Functions defined in the file.
    assert ("function", "FM_AppInit") in syms
    assert ("function", "FM_AppMain") in syms
    # Type declarations.
    assert ("class", "FM_Data") in syms
    assert ("class", "FM_Global_t") in syms
    assert ("class", "FM_State") in syms
    # NOT calls, control flow, or a prototype (ends in ';').
    names = {n for _, n in syms}
    assert "FM_ChildInit" not in names  # a call
    assert "DoWork" not in names        # a call
    assert "running" not in names       # while-condition
    assert "FM_Prototype" not in names  # a declaration, not a definition


def test_cpp_language_shares_the_fallback():
    syms = {s.name for s in extract_symbols("w.cpp", "void Widget::draw() const {\n}\n", "C++")}
    # C++ out-of-line method definitions keep their qualified name.
    assert "Widget::draw" in syms


def test_root_level_dirs_are_ignored():
    globs = [".secagent/**", "**/node_modules/**", "**/dist/**", "**/build/**"]
    # Root-level copies (the usual layout) must be ignored...
    assert _ignored("node_modules/react/index.js", globs)
    assert _ignored("dist/bundle.js", globs)
    assert _ignored("build/x.o", globs)
    # ...and nested copies still are.
    assert _ignored("pkg/node_modules/y.js", globs)
    assert _ignored("src/build/foo.c", globs)
    # But similarly-named real source is NOT over-matched.
    assert not _ignored("buildscript.py", globs)
    assert not _ignored("rebuild/x.c", globs)
    assert not _ignored("src/app.c", globs)


# --- C header declarations ----------------------------------------------------
# Devs in TWO separate agentic cohorts lost time to `find-symbol` answering
# "No symbols matching 'FM_GET_FILE_INFO_CC'" — worded identically to "does not
# exist" — for macros and typedefs that plainly exist. In C flight software the
# command codes, message structs and MIDs ARE the wiring, and they live only in
# headers.

_HEADER = """\
#ifndef FM_MSG_H
#define FM_MSG_H

#define FM_GET_FILE_INFO_CC FM_CCVAL(GET_FILE_INFO)
#define FM_TABLE_ENTRY_COUNT 64

typedef struct
{
    uint32 Count;
} FM_GetFileInfoCmd_t;

typedef uint32 FM_Id_t;

#endif
"""


def test_c_header_macros_and_typedefs_are_indexed():
    syms = {s.name: s.kind for s in extract_symbols("fm_msg.h", _HEADER, "C")}
    assert syms.get("FM_GET_FILE_INFO_CC") == "constant"
    assert syms.get("FM_TABLE_ENTRY_COUNT") == "constant"
    assert syms.get("FM_GetFileInfoCmd_t") == "class"   # anonymous typedef struct
    assert syms.get("FM_Id_t") == "class"               # one-line typedef


def test_include_guards_are_not_indexed_as_api():
    """`#define FM_MSG_H` is structure, not API — it would be pure noise in find-symbol."""
    names = {s.name for s in extract_symbols("fm_msg.h", _HEADER, "C")}
    assert "FM_MSG_H" not in names


def test_header_indexing_does_not_break_c_source_extraction():
    src = (
        "struct FM_Data { int x; };\n"
        "int32 FM_AppInit(uint32 x)\n{\n    FM_ChildInit();\n    return 0;\n}\n"
    )
    syms = {(s.kind, s.name) for s in extract_symbols("fm.c", src, "C")}
    assert ("function", "FM_AppInit") in syms
    assert ("class", "FM_Data") in syms
    assert "FM_ChildInit" not in {n for _, n in syms}   # still not a call


# --- C++ class declarations ---------------------------------------------------
# Found on first contact with a real C++ codebase (PX4-GPSDrivers): find-symbol could
# not see a single class. Two runs on plain-C cFS never exposed it — the pattern
# required `{`, `;` or EOL right after the name, so every DERIVED class was missed, and
# `class` was not even in the keyword list.

def test_cpp_class_declarations_are_indexed():
    for src, expected in (
        ("class GPSBaseStationSupport : public GPSHelper\n", "GPSBaseStationSupport"),
        ("class Foo final : public Bar\n", "Foo"),        # `final` must not be captured
        ("class Simple\n", "Simple"),
        ("class Widget;\n", "Widget"),                     # forward declaration
        ("struct Plain {\n", "Plain"),
        ("enum Color { RED };\n", "Color"),
    ):
        names = {s.name for s in extract_symbols("x.h", src, "C++") if s.kind == "class"}
        assert expected in names, f"{expected!r} missing from {names} for {src!r}"


def test_cpp_class_pattern_does_not_match_statements():
    for src in ("    if (x) {\n", "  return foo();\n", "  } else {\n", "x = class_id;\n"):
        assert not extract_symbols("x.cpp", src, "C++"), src
