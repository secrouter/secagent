"""Signal detectors must not fire on commented-out or documented wiring, and the
heuristic purpose must skip license/copyright/guard boilerplate. Regression coverage
for the extraction-layer review findings."""

from __future__ import annotations

from secagent.affordances.file_summary import heuristic_purpose, summarize_file
from secagent.affordances.signals import find_endpoints, find_outbound, strip_comments


def test_strip_comments_preserves_string_urls():
    code = 'x = get("http://real.example/api")  // see http://doc.example/wiki\n'
    s = strip_comments(code)
    assert "real.example" in s          # in-string URL survives
    assert "doc.example" not in s       # commented URL removed


def test_strip_comments_handles_block_and_hash():
    code = 'a=1  # os.environ["SECRET"]\n/* app.get("/dead") */\nb=2\n'
    s = strip_comments(code)
    assert "SECRET" not in s
    assert "/dead" not in s
    assert "a=1" in s and "b=2" in s


def test_outbound_ignores_commented_url_keeps_real():
    code = 'requests.get("http://real.example/api")  # http://doc.example/x\n'
    out = find_outbound(strip_comments(code))
    assert out == ["real.example"]


def test_endpoints_ignore_commented_route():
    js = '// app.get("/old-route", h)\napp.post("/live", handler)\n'
    assert find_endpoints(strip_comments(js)) == ["/live"]


def test_summarize_file_does_not_pick_up_commented_signals():
    code = (
        'import os\n'
        '# leftover: app.get("/legacy")\n'
        '# old = os.environ["OLD_TOKEN"]\n'
        'def h():\n'
        '    return requests.get("http://live.example/v1")\n'
    )
    s = summarize_file("svc.py", code, "Python", [])
    assert s.endpoints == []            # the commented route is gone
    assert "OLD_TOKEN" not in s.env_vars
    assert s.outbound_calls == ["live.example"]  # the real call survives


def test_heuristic_purpose_skips_license_and_guard_boilerplate():
    hdr = (
        "/*\n"
        " * Copyright 2020 NASA. All Rights Reserved.\n"
        " * SPDX-License-Identifier: Apache-2.0\n"
        " */\n"
        "#ifndef FM_APP_H\n"
        "#define FM_APP_H\n"
        "/* File Manager application - defines the public interface. */\n"
    )
    purpose = heuristic_purpose("fm_app.h", hdr, "C", [])
    # Picks the real description, not the copyright/SPDX/include-guard lines.
    assert purpose == "File Manager application - defines the public interface."


# --- license-block purpose detection -----------------------------------------
# Found by two independent devs testing secagent against NASA cFS: every file in the
# repo reported its purpose as "NASA Docket No". The old filter judged comments
# line-by-line, and a NASA license notice opens with a line carrying no license
# keyword at all ("NASA Docket No. GSC-19,200-1, ...") — copyright appears three
# lines later — so the first line passed the filter and became the purpose.

_NASA_HEADER = (
    "/************************************************************************\n"
    " * NASA Docket No. GSC-19,200-1, and identified as \"cFS Draco\"\n"
    " *\n"
    " * Copyright (c) 2023 United States Government as represented by the\n"
    " * Administrator of the National Aeronautics and Space Administration.\n"
    " * All Rights Reserved.\n"
    " *\n"
    " * Licensed under the Apache License, Version 2.0 (the \"License\");\n"
    " ************************************************************************/\n"
    "\n"
    "/**\n"
    " * @file\n"
    " *  File Manager (FM) Application Ground Commands\n"
    " *\n"
    " *  Provides functions for the execution of the FM ground commands\n"
    " */\n"
    "\n"
    '#include "fm_tbl.h"\n'
)


def test_license_block_is_discarded_whole_not_line_by_line():
    p = heuristic_purpose("fm_cmds.c", _NASA_HEADER, "C", [])
    assert p == "File Manager (FM) Application Ground Commands"
    for junk in ("NASA Docket", "Copyright", "Apache", "Rights Reserved"):
        assert junk not in p


def test_bare_doxygen_tag_is_not_the_purpose():
    """`@file` opens the real comment but carries no description of its own."""
    src = "/**\n * @file\n *  Does the thing\n */\n"
    assert heuristic_purpose("x.c", src, "C", []) == "Does the thing"


def test_doxygen_brief_prefix_is_stripped_but_prose_kept():
    src = "/**\n * @brief Dispatches ground commands\n */\n"
    assert heuristic_purpose("x.c", src, "C", []) == "Dispatches ground commands"


def test_structural_doxygen_metadata_is_skipped():
    src = "/**\n * @ingroup fmapp\n * @param Msg the command\n *  Real description here\n */\n"
    assert heuristic_purpose("x.c", src, "C", []) == "Real description here"


def test_plain_header_comment_still_works():
    """No license, no doxygen — the first real comment is still the purpose."""
    src = "/* Utility helpers for path handling */\nint f(void);\n"
    assert heuristic_purpose("x.c", src, "C", []) == "Utility helpers for path handling"


# --- key_symbols truncation ---------------------------------------------------
# Both cohort-2 devs independently treated summary.key_symbols as a file's COMPLETE
# API and missed the functions they were looking for: fm_cmds.c has 20 functions,
# key_symbols showed 12, and the 8 hidden ones were the directory commands one of
# them needed. The cap is fine; hiding the fact that it capped is not.

def _syms(n):
    from secagent.affordances.models import Symbol
    return [Symbol(f"fn{i}", "function", "a.c", i, f"void fn{i}(void)") for i in range(n)]


def test_key_symbols_announces_truncation():
    from secagent.affordances.file_summary import _MAX_KEY_SYMBOLS, summarize_file

    s = summarize_file("a.c", "int x;\n", "C", _syms(20))
    assert len(s.key_symbols) == _MAX_KEY_SYMBOLS + 1        # cap + one note
    note = s.key_symbols[-1]
    assert "+8 more" in note                                  # 20 - 12
    assert "functions" in note                                # points at the full list


def test_key_symbols_silent_when_nothing_hidden():
    from secagent.affordances.file_summary import summarize_file

    s = summarize_file("a.c", "int x;\n", "C", _syms(5))
    assert len(s.key_symbols) == 5
    assert not any("more not shown" in k for k in s.key_symbols)
