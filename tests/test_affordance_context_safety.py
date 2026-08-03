"""Affordance context-safety: list-shaped outputs stay bounded, and read_slice refuses
secret files + streams instead of loading whole files. Regression coverage for the
affordance-query review findings."""

from __future__ import annotations

import json

from secagent.affordances import queries
from secagent.affordances.models import CallEdge, FileRecord, FileSummary, Symbol
from secagent.affordances.store import AffordanceStore


def test_callers_output_is_capped(tmp_path):
    """A hot symbol with thousands of callers must not flood the agent's context."""
    n = queries._MAX_ROWS + 50
    edges = [
        CallEdge(f"caller_{i}.c", "target.c", caller=f"fn_{i}", callee="Hot")
        for i in range(n)
    ]
    store = AffordanceStore(tmp_path, ".secagent")
    try:
        store.set_call_map(edges)
        store.commit()
        out = json.loads(queries.callers(store, "Hot"))
    finally:
        store.close()
    # Capped to _MAX_ROWS rows + one trailing note element.
    assert len(out) == queries._MAX_ROWS + 1
    assert "note" in out[-1] and "more callers" in out[-1]["note"]


def test_functions_output_is_capped(tmp_path):
    n = queries._MAX_ROWS + 10
    syms = [
        Symbol(f"fn_{i}", "function", "big.c", i, f"void fn_{i}(void)", "", "", "")
        for i in range(n)
    ]
    store = AffordanceStore(tmp_path, ".secagent")
    try:
        store.upsert_file(
            FileRecord(path="big.c", language="C", size=1, sha256="x", loc=1, n_symbols=n),
            FileSummary(path="big.c"),
            syms,
        )
        store.commit()
        out = json.loads(queries.functions(store, "big.c"))
    finally:
        store.close()
    assert len(out) == queries._MAX_ROWS + 1
    assert "more functions" in out[-1]["note"]


def test_read_slice_refuses_secret_files(tmp_path):
    (tmp_path / ".env").write_text("API_KEY=super-secret\n")
    (tmp_path / "server.pem").write_text("-----BEGIN PRIVATE KEY-----\n")
    (tmp_path / "app.py").write_text("x = 1\ny = 2\nz = 3\n")
    store = AffordanceStore(tmp_path, ".secagent")
    try:
        assert queries.read_slice(store, ".env", 1, 5).startswith("ERROR")
        assert "secret" in queries.read_slice(store, ".env", 1, 5)
        assert queries.read_slice(store, "server.pem", 1, 5).startswith("ERROR")
        # A normal source file still reads fine.
        ok = queries.read_slice(store, "app.py", 1, 2)
        assert "1: x = 1" in ok and "2: y = 2" in ok and "z = 3" not in ok
    finally:
        store.close()


def test_read_slice_streams_only_the_window(tmp_path):
    """read_slice must return the right lines without depending on reading the whole
    file (it now streams). Verify a mid-file window on a large file."""
    big = tmp_path / "big.txt"
    big.write_text("\n".join(f"line{i}" for i in range(1, 10001)) + "\n")
    store = AffordanceStore(tmp_path, ".secagent")
    try:
        out = queries.read_slice(store, "big.txt", 5000, 5002)
    finally:
        store.close()
    assert out == "5000: line5000\n5001: line5001\n5002: line5002"
