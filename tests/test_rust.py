"""Rust support: regex symbols + the tree-sitter call map (the C#/clang analog)."""

from __future__ import annotations

import pytest

from secagent.affordances import signals
from secagent.affordances.call_map import build_definition_table, resolve_calls
from secagent.affordances.file_summary import summarize_file
from secagent.affordances.io_map import (
    build_io_map,
    build_rust_module_index,
    resolve_rust_import,
    rust_module_name_for,
)
from secagent.affordances.languages import detect_language
from secagent.affordances.models import FileRecord
from secagent.affordances.rust_ast import parse_rust_file, rust_available
from secagent.affordances.symbols import extract_symbols, rust_imports


def test_rust_detected_as_code():
    from pathlib import Path

    assert detect_language(Path("src/lib.rs")) == "Rust"


def test_rust_regex_symbols_cover_free_fns_impl_methods_and_structs():
    src = (
        "pub fn init() {}\n"
        "struct Server { port: u16 }\n"
        "impl Server {\n"
        "    pub fn new(port: u16) -> Self { Server { port } }\n"
        "    fn run(&self) {}\n"
        "}\n"
    )
    kinds = {s.name: s.kind for s in extract_symbols("lib.rs", src, "Rust")}
    # `fn` matches free functions AND impl methods (both are `fn` lines).
    assert kinds.get("init") == "function"
    assert kinds.get("new") == "function"
    assert kinds.get("run") == "function"
    assert kinds.get("Server") == "class"


def test_rust_available_is_graceful():
    # Never raises; returns a bool regardless of whether the grammar is installed.
    assert isinstance(rust_available(), bool)


def test_rust_imports_extraction():
    src = (
        "use std::collections::HashMap;\n"
        "use crate::db::{connect, Query};\n"
        "use crate::util::log as l;\n"
        "use super::config::Settings;\n"
        "use self::helpers::*;\n"
        "pub mod db;\n"
        "mod handlers;\n"
        "mod inline { fn x() {} }\n"  # inline module: no separate file, must be ignored
    )
    imps = rust_imports(src)
    assert "crate::db::connect" in imps and "crate::db::Query" in imps  # group expanded
    assert "crate::util::log" in imps          # `as l` alias stripped
    assert "super::config::Settings" in imps
    assert "self::helpers" in imps             # `*` glob dropped
    assert "std::collections::HashMap" in imps
    assert "self::db" in imps and "self::handlers" in imps  # mod decls
    assert not any("inline" in i for i in imps)            # inline mod ignored


def test_rust_module_name_and_resolution():
    assert rust_module_name_for("src/lib.rs") == "crate"
    assert rust_module_name_for("src/db/mod.rs") == "crate::db"
    assert rust_module_name_for("src/db/pool.rs") == "crate::db::pool"
    assert rust_module_name_for("crate_a/src/foo.rs") == "crate_a::foo"  # workspace crate
    assert rust_module_name_for("src/main.py") is None

    recs = [FileRecord(path=p, language="Rust", size=1, sha256=p, loc=1, n_symbols=0)
            for p in ["src/main.rs", "src/db/mod.rs", "src/config.rs"]]
    idx = build_rust_module_index(recs)
    # crate:: rebases onto the importer's crate; item inside a module resolves to it.
    assert resolve_rust_import("crate::db::connect", "crate", idx) == "src/db/mod.rs"
    # super:: walks up from the importer module.
    assert resolve_rust_import("super::config", "crate::handlers", idx) == "src/config.rs"
    # external crate -> no internal edge.
    assert resolve_rust_import("std::fs::File", "crate", idx) is None


def test_rust_import_edges_in_io_map():
    files = {
        "src/main.rs": "mod db;\nuse crate::db::connect;\nfn main() { connect(); }\n",
        "src/db/mod.rs": "pub fn connect() {}\n",
    }
    records, summaries = [], {}
    for path, text in files.items():
        rec = FileRecord(path=path, language="Rust", size=len(text), sha256=path,
                         loc=text.count("\n"), n_symbols=0)
        records.append(rec)
        summaries[path] = summarize_file(path, text, "Rust", [])  # populates .imports
    # summarize_file wires rust_imports into the summary.
    assert "crate::db::connect" in summaries["src/main.rs"].imports
    _components, edges = build_io_map(records, summaries)
    import_edges = {(e.src, e.dst) for e in edges if e.kind == "import"}
    assert ("src", "db") in import_edges


def test_rust_io_signals():
    src = (
        'use std::env;\n'
        '#[get("/health")]\n'
        'async fn health() -> String {\n'
        '    let key = env::var("API_KEY").unwrap();\n'
        '    let r = reqwest::Client::new().get("https://api.stripe.com/v1").send();\n'
        '    let app = Router::new().route("/users", get(list_users));\n'
        '    let pool = sqlx::PgPool::connect(&url);\n'
        '    let conn = rusqlite::Connection::open("x.db");\n'
        '    let ch = lapin::Connection::connect(addr);\n'
        '    let sock = std::net::UdpSocket::bind("0.0.0.0:0");\n'
        '    let listener = TcpListener::bind(addr);\n'
        '}\n'
    )
    assert set(signals.find_endpoints(src)) >= {"/health", "/users"}
    assert signals.find_env_vars(src) == ["API_KEY"]
    assert signals.find_outbound(src) == ["api.stripe.com"]
    ds = signals.find_datastores(src)
    assert "SQLx" in ds and "SQLite" in ds
    assert "RabbitMQ/AMQP" in signals.find_messaging(src)
    assert set(signals.find_sockets(src)) == {"TCP socket", "UDP socket"}


@pytest.mark.skipif(not rust_available(), reason="tree-sitter Rust grammar not installed")
def test_rust_treesitter_call_map(tmp_path):
    (tmp_path / "helper.rs").write_text(
        "pub fn greet(name: &str) -> String { format!(\"hi {}\", name) }\n"
        "pub fn log_line(s: &str) { println!(\"{}\", s); }\n"
    )
    (tmp_path / "main.rs").write_text(
        "mod helper;\n"
        "fn process(input: &str) {\n"
        "    let g = helper::greet(input);\n"
        "    helper::log_line(&g);\n"
        "}\n"
        "fn main() { process(\"world\"); }\n"
    )

    uh = parse_rust_file(tmp_path / "helper.rs", "helper.rs")
    um = parse_rust_file(tmp_path / "main.rs", "main.rs")
    assert uh.parsed and um.parsed

    funcs = uh.functions + um.functions
    calls = uh.calls + um.calls
    # Free fns + impl methods are all function_item.
    assert {f.name for f in funcs} >= {"greet", "log_line", "process", "main"}
    # Calls are attributed to their enclosing function.
    assert ("process", "greet") in {(c.caller, c.callee) for c in calls}

    edges = resolve_calls(calls, build_definition_table(funcs))
    resolved = {(e.src_file, e.dst_file, e.callee) for e in edges}
    # Cross-file edge: main.rs -> helper.rs via greet (scoped_identifier callee).
    assert ("main.rs", "helper.rs", "greet") in resolved
    assert ("main.rs", "helper.rs", "log_line") in resolved
