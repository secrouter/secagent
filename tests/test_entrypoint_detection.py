"""Entrypoint detection (from the cFS docs review).

cFS reported *no* entrypoints because detection was filename-only (main.py/index.js), but
cFS apps' entrypoints are *functions* (``FM_AppMain``). Detection now also recognizes
``main`` / ``*Main`` entrypoint functions from the symbol table.
"""

from __future__ import annotations

from secagent.affordances.project_map import entrypoint_files_from_symbols


def test_framework_entrypoint_functions_detected():
    syms = [
        ("FM_AppMain", "apps/fm/fsw/src/fm_app.c"),
        ("FM_AppInit", "apps/fm/fsw/src/fm_app.c"),
        ("CFE_PSP_Main", "psp/fsw/src/cfe_psp_start.c"),
        ("main", "tools/cmd/main.c"),
    ]
    assert entrypoint_files_from_symbols(syms) == {
        "apps/fm/fsw/src/fm_app.c",
        "psp/fsw/src/cfe_psp_start.c",
        "tools/cmd/main.c",
    }


def test_lowercase_main_suffix_not_matched():
    # "*Main" is CamelCase-anchored, so words ending in lowercase "main" don't match.
    syms = [("domain", "a.c"), ("remain", "b.c"), ("FM_GetOpenFilesCmd", "c.c")]
    assert entrypoint_files_from_symbols(syms) == set()


def test_test_stub_entrypoints_excluded():
    # A unit-test stub defining FM_AppMain is a double, not the real entrypoint.
    syms = [
        ("FM_AppMain", "apps/fm/fsw/src/fm_app.c"),
        ("FM_AppMain", "apps/fm/unit-test/stubs/fm_app_stubs.c"),
    ]
    assert entrypoint_files_from_symbols(syms) == {"apps/fm/fsw/src/fm_app.c"}
