"""A file's purpose must survive the comment convention its language actually uses.

secagent's heuristic purpose extractor was line-oriented and C/C++-shaped: it only saw
comment lines carrying their own prefix (`//`, `*`, `#`). Two languages write their file
docs in ways that produced confidently wrong descriptions rather than none:

- **Rust** puts module docs in `/*! ... */` with bare prose on the continuation lines.
  The block was abandoned at the delimiter, so extraction fell through to whatever
  comment appeared later in the file. On aho-corasick that made `lib.rs` read "Overview"
  (a section heading) and `automaton.rs` read "We seal the `Automaton` trait for now" —
  a note about a private internal detail, presented as the file's purpose.
- **C#** puts the XML doc tag on its own line, so stripping `///` leaves a bare
  `<summary>`. Five of five spot-checked eShop files had the literal string "<summary>"
  as their description.

Both feed `components`, `search`, `context` and the generated docs, so a wrong purpose
propagates into everything built on top of it.
"""

from __future__ import annotations

from secagent.affordances.file_summary import heuristic_purpose

RUST_BLOCK = '''/*!
Provides [`Automaton`] trait for abstracting over Aho-Corasick automata.

The `Automaton` trait provides a way to write generic code.
*/

use core::mem;

/// We seal the `Automaton` trait for now.
pub trait Automaton {}
'''

RUST_LINE = '''//! A library for finding occurrences of many patterns at once.
//!
//! More detail here.

pub struct AhoCorasick;
'''

CSHARP = '''using System;

namespace Demo;

/// <summary>
/// Represents a snapshot of the item that was ordered.
/// </summary>
public class CatalogItemOrdered { }
'''

CSHARP_INLINE = '''/// <summary>Specific query used to fetch the count.</summary>
public interface IBasketQueryService { }
'''


def test_rust_block_module_doc_is_the_purpose():
    got = heuristic_purpose("src/automaton.rs", RUST_BLOCK, "Rust", [])
    assert got.startswith("Provides [`Automaton`] trait"), got
    assert "seal" not in got, "fell through to a later comment about a private detail"
    assert "/*" not in got and got[:1] != "!"


def test_rust_line_module_doc_is_the_purpose():
    got = heuristic_purpose("src/lib.rs", RUST_LINE, "Rust", [])
    assert got.startswith("A library for finding occurrences"), got


def test_csharp_xml_summary_tag_is_not_the_purpose():
    got = heuristic_purpose("Entities/CatalogItemOrdered.cs", CSHARP, "C#", [])
    assert got.startswith("Represents a snapshot"), got
    assert "<summary>" not in got


def test_csharp_inline_summary_is_unwrapped():
    got = heuristic_purpose("Interfaces/IBasketQueryService.cs", CSHARP_INLINE, "C#", [])
    assert got.startswith("Specific query used to fetch"), got
    assert "<" not in got


# --- the C/C++ behaviour this must not disturb --------------------------------

C_STARRED = '''/*
 * fm_app.c
 *
 * Core File Manager application entry point.
 */
#include "fm_app.h"
'''

C_LICENSE_THEN_DESC = '''/*
 * Copyright (c) 2020 United States Government as represented by NASA.
 * Licensed under the Apache License, Version 2.0.
 */

/* Child task queue handling for the File Manager. */

#include "fm_child.h"
'''


def test_c_starred_block_still_works():
    """Also covers a long-standing wart in the same family: an embedded C header that
    opens with its own filename yielded "fm_app.c" as the description — a confident
    answer carrying no information."""
    got = heuristic_purpose("fm_app.c", C_STARRED, "C", [])
    assert "File Manager application entry point" in got, got
    assert got != "fm_app.c"


def test_license_block_is_still_separable_from_the_description():
    """A blank line must still end a block, or the license notice and the real header
    merge and the description gets dropped as boilerplate with it."""
    got = heuristic_purpose("fm_child.c", C_LICENSE_THEN_DESC, "C", [])
    assert "Child task queue handling" in got, got
    assert "Copyright" not in got and "Licensed" not in got


def test_unterminated_block_comment_does_not_swallow_the_file():
    """Defensive: a file whose block comment is never closed must not turn every
    subsequent line of code into 'comment text'."""
    text = "/*\nDescription here.\n" + "\n".join(f"int v{i};" for i in range(40))
    got = heuristic_purpose("weird.c", text, "C", [])
    assert "int v0" not in got


# --- Python has docstrings; it should not be mined for stray comments ---------

PY_NO_DOCSTRING = '''from __future__ import annotations

# Reserved storage name of the automatic help option.
_HELP = "help"


class Context:
    """The execution context."""


class Command:
    """A callable command."""


def _private():
    pass


def echo(message):
    """Print a message."""
'''

PY_WITH_DOCSTRING = '''"""Utilities for printing to the terminal."""

def echo(message):
    pass
'''


def test_python_module_without_a_docstring_is_described_by_its_definitions():
    """Click's core.py — the module defining Command, Group, Context, Option and
    Argument — was described as "Reserved storage name of the automatic help option",
    a remark about an internal constant, stated with complete confidence.

    Python documents modules with docstrings, so a comment further down is about a local
    detail. The module's own top-level definitions cannot be wrong, and they name what a
    reader is actually looking for."""
    got = heuristic_purpose("click/core.py", PY_NO_DOCSTRING, "Python", [])
    assert "Reserved storage" not in got, got
    assert "Context" in got and "Command" in got
    assert "echo" in got


def test_private_definitions_are_not_advertised():
    got = heuristic_purpose("click/core.py", PY_NO_DOCSTRING, "Python", [])
    assert "_private" not in got and "_HELP" not in got


def test_a_real_module_docstring_still_wins():
    got = heuristic_purpose("click/utils.py", PY_WITH_DOCSTRING, "Python", [])
    assert got.startswith("Utilities for printing")


def test_a_python_file_with_nothing_to_say_stays_honest():
    """No docstring and no public definitions: say what it is, do not invent."""
    got = heuristic_purpose("pkg/__init__.py", "import os\nX = 1\n", "Python", [])
    assert "Python file" in got
