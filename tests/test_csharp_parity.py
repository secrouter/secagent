"""Two C# parity defects, both found by measuring eShop with the real tool:

1. eShop reported *zero* entrypoints despite having three (`src/Web/Program.cs`,
   `src/PublicApi/Program.cs`, `src/BlazorAdmin/Program.cs`). All three use .NET 6+
   top-level statements, which compile to an implicit `Main` with no `Main` *symbol* at
   all -- so neither of `project_map.py`'s two entrypoint mechanisms (the
   `_ENTRYPOINT_NAMES` filename set, the `_ENTRYPOINT_SYMBOL_RE` symbol regex) caught it.
   The fix adds `Program.cs` to the filename set, the same convention-based mechanism
   `main.py`/`index.js` already use.

2. A degraded (`build.restored: false`) heavy-analysis run was invisible in the JSON
   `analyze deep` prints -- `heavy.py` logs a warning to stderr, but `ingest_report`'s
   return value (what the CLI actually prints, and what the pi extension forwards
   verbatim into a model's context) didn't carry it. See `tests/test_heavy_analysis.py`
   for that fix's tests; this file covers only the entrypoint-detection defect, whose
   fixture (a real eShop `Program.cs`) belongs here.
"""

from __future__ import annotations

from pathlib import Path

from secagent.affordances.models import FileRecord
from secagent.affordances.project_map import (
    _ENTRYPOINT_SYMBOL_RE,
    build_project_map,
    entrypoint_files_from_symbols,
)
from secagent.affordances.symbols import extract_symbols

FIXTURE = Path(__file__).parent / "fixtures" / "csharp" / "Program.cs"


def _record(path: str, text: str) -> FileRecord:
    return FileRecord(path=path, language="C#", size=len(text), sha256="x",
                       loc=text.count("\n") + 1)


def test_real_top_level_statements_program_cs_has_no_main_symbol():
    """Ground truth for *why* the symbol-based path can't see this: eShop's real
    Program.cs (top-level statements, .NET 6+, vendored in
    tests/fixtures/csharp/Program.cs) defines no `Main` symbol at all, so the
    symbol-regex detector agrees there is nothing here to find."""
    text = FIXTURE.read_text(encoding="utf-8")
    syms = extract_symbols("src/Web/Program.cs", text, "C#")
    assert not any(_ENTRYPOINT_SYMBOL_RE.search(s.name) for s in syms)
    assert entrypoint_files_from_symbols(
        [(s.name, "src/Web/Program.cs") for s in syms]) == set()


def test_program_cs_detected_as_entrypoint_by_filename():
    """The fix: filename-based detection now covers .NET's Program.cs convention, so a
    project with zero Main symbols (eShop's real shape, all three of its entrypoints)
    still reports them. A non-entrypoint .cs file in the same project is not swept in."""
    text = FIXTURE.read_text(encoding="utf-8")
    records = [
        _record("src/Web/Program.cs", text),
        _record("src/PublicApi/Program.cs", text),
        _record("src/BlazorAdmin/Program.cs", text),
        _record("src/Web/Startup.cs", "namespace Web { class Startup {} }"),
    ]
    pm = build_project_map(".", records)
    assert pm.entrypoints == [
        "src/BlazorAdmin/Program.cs",
        "src/PublicApi/Program.cs",
        "src/Web/Program.cs",
    ]


def test_no_program_cs_means_no_entrypoint():
    """Silence test: a C# project with no Program.cs (e.g. a class library) reports
    none -- the fix is a filename convention, not a blanket match on every `*.cs`."""
    records = [_record("src/Lib/Widget.cs", "namespace Lib { class Widget {} }")]
    pm = build_project_map(".", records)
    assert pm.entrypoints == []


def test_program_cs_with_an_explicit_main_is_still_one_entrypoint_not_two():
    """Attack the mechanism: a classic-style Program.cs (explicit `static void Main`,
    pre-.NET-6 style) is caught by BOTH detectors -- the filename set here, and the
    Main-symbol regex feeding `entrypoint_hints` from the caller (see api.py). It must
    still surface as exactly one entrypoint, not two."""
    text = (
        "namespace Demo\n{\n    public class Program\n    {\n"
        "        public static void Main(string[] args) { }\n    }\n}\n"
    )
    records = [_record("src/Classic/Program.cs", text)]
    hints = entrypoint_files_from_symbols([("Main", "src/Classic/Program.cs")])
    assert hints == {"src/Classic/Program.cs"}  # sanity: the symbol path also catches it

    pm = build_project_map(".", records, entrypoint_hints=hints)
    assert pm.entrypoints == ["src/Classic/Program.cs"]
