"""C#/.NET support: regex symbols, .NET IO signals, and the tree-sitter call map."""

from __future__ import annotations

import pytest

from secagent.affordances import signals
from secagent.affordances.call_map import build_definition_table, resolve_calls
from secagent.affordances.csharp_ast import (
    csharp_available,
    is_generated_csharp,
    parse_csharp_file,
)
from secagent.affordances.languages import detect_language
from secagent.affordances.symbols import extract_symbols

_CS = """
namespace Foo
{
    public class Widget
    {
        public int Add(int a, int b) { return a + b; }
        private void Helper() { if (a) { return; } }
    }
    public interface IThing { }
    public enum Color { Red, Green }
}
"""


def test_csharp_regex_symbols():
    syms = extract_symbols("Widget.cs", _CS, "C#")
    kinds = {s.name: s.kind for s in syms}
    assert kinds.get("Widget") == "class"
    assert kinds.get("IThing") == "class"      # interfaces/enums surface as "class"
    assert kinds.get("Color") == "class"
    assert kinds.get("Add") == "method"
    # control-flow keywords must not be mistaken for declarations
    assert "if" not in kinds and "return" not in kinds


def test_dotnet_io_signals():
    text = """
    private readonly IConfiguration Configuration;
    [HttpGet("/api/widgets")]
    public IActionResult Get()
    {
        app.MapPost("/things", Handler);
        var key = Environment.GetEnvironmentVariable("API_KEY");
        var db = Configuration["ConnectionStrings:Default"];
        using var conn = new SqlConnection(cs);
        services.AddDbContext<AppContext>();
    }
    """
    endpoints = signals.find_endpoints(text)
    assert "/api/widgets" in endpoints
    assert "/things" in endpoints
    assert "API_KEY" in signals.find_env_vars(text)
    assert "ConnectionStrings:Default" in signals.find_env_vars(text)
    stores = signals.find_datastores(text)
    assert "SQL Server" in stores
    assert "Entity Framework" in stores


def test_configuration_indexer_needs_iconfiguration_context():
    """A member merely named `Configuration` (e.g. a DTO indexer) is NOT an env var —
    the .NET config indexer only counts when the file actually uses IConfiguration."""
    dto = 'var theme = record.Configuration["theme"];\n'  # no IConfiguration in the file
    assert signals.find_env_vars(dto) == []
    real = ('IConfiguration Configuration;\n'
            'var x = Configuration["Auth:Key"];\n')
    assert signals.find_env_vars(real) == ["Auth:Key"]


def test_project_files_detected():
    from pathlib import Path
    assert detect_language(Path("App.csproj")) == "MSBuild"
    assert detect_language(Path("Solution.sln")) == "MSBuild"


def test_is_generated_csharp():
    assert is_generated_csharp("Form1.Designer.cs")
    assert is_generated_csharp("obj/App.g.cs")
    assert is_generated_csharp("Properties/AssemblyInfo.cs")
    assert not is_generated_csharp("Widget.cs")


@pytest.mark.skipif(not csharp_available(), reason="tree-sitter C# grammar not installed")
def test_csharp_treesitter_call_map(tmp_path):
    (tmp_path / "A.cs").write_text(
        "public class A { public void Run() { var b = new B(); b.Work(); } }")
    (tmp_path / "B.cs").write_text(
        "public class B { public void Work() { } }")

    ua = parse_csharp_file(tmp_path / "A.cs", "A.cs")
    ub = parse_csharp_file(tmp_path / "B.cs", "B.cs")
    assert ua.parsed and ub.parsed

    funcs = ua.functions + ub.functions
    calls = ua.calls + ub.calls
    assert {f.name for f in funcs} >= {"Run", "Work"}

    edges = resolve_calls(calls, build_definition_table(funcs))
    assert ("A.cs", "B.cs", "Work") in {(e.src_file, e.dst_file, e.callee) for e in edges}
