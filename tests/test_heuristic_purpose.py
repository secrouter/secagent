"""heuristic_purpose must skip decorative banner comments (cFS docs review).

cFS headers open with a ``/**********`` banner; the heuristic was grabbing the row of
asterisks as the file's purpose, which then rendered as ``\\*\\*\\*…`` in the docs.
"""

from __future__ import annotations

from secagent.affordances.file_summary import heuristic_purpose


def test_banner_comment_skipped_and_stripped():
    src = (
        "/" + "*" * 60 + "\n"
        "** File: mm_utils.h\n"
        "** Purpose: MM utilities\n"
        "*/\n"
        "void MM_Foo(void);\n"
    )
    # Banner row skipped; leading "** " stripped off the next real line.
    assert heuristic_purpose("apps/mm/fsw/src/mm_utils.h", src, "C", []) == "File: mm_utils.h"


def test_pure_banner_does_not_leak_asterisks():
    src = "/" + "*" * 40 + "\n" + "*" * 40 + "\n*/\n"
    p = heuristic_purpose("a/b/x.h", src, "C", [])
    assert "*" not in p  # falls back to the filename, not the banner


def test_real_comment_still_used():
    src = "// Compute the checksum of a memory region\nint cs(void);\n"
    assert heuristic_purpose("a.c", src, "C", []).startswith("Compute the checksum")
