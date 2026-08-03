"""An MCP client must be able to refresh the index it queries.

Every affordance answers from the index, never from disk. So an agent that edits a file
and then asks about it gets the file as it *was* — returned in the ordinary shape, with
nothing marking it stale. That is the same silent-wrongness class as the rest of this
suite, with an extra sting: over MCP there was no `reindex` tool at all, so the only fix
was to leave the client and run `secagent index` in a shell — which an MCP client may not
have. Reported from a Kilo Code session that edited code and kept getting the old answers.
"""

from __future__ import annotations

import json

from secagent.affordances.api import index_repo
from secagent.affordances.store import AffordanceStore
from secagent.affordances.tools import build_affordance_tools
from secagent.config import Settings


def _settings() -> Settings:
    s = Settings()
    s.affordances.llm_summaries = False
    return s


def _repo(tmp_path, body: str):
    (tmp_path / "mod.py").write_text(body)
    return tmp_path


def _found(out: str) -> set[str]:
    """Symbol names in a find_symbol result.

    Parsed rather than substring-matched: the "not found" note echoes the name you
    asked for, so `"beta" in out` is true even when nothing was found.
    """
    data = json.loads(out)
    if isinstance(data, dict):          # a {"note": ...} miss
        return set()
    return {row.get("name", "") for row in data if isinstance(row, dict)}


def test_reindex_makes_edits_visible(tmp_path):
    """The end-to-end bug: edit a file, and queries keep serving the old version."""
    _repo(tmp_path, "def alpha():\n    return 1\n")
    settings = _settings()
    index_repo(tmp_path, settings)

    store = AffordanceStore(tmp_path, ".secagent")
    try:
        tools = build_affordance_tools(store, settings=settings)
        call = {t.name: t for t in tools.specs()}

        assert "alpha" in _found(tools.call("find_symbol", {"name": "alpha"}))
        assert "reindex" in call, "MCP clients need a way to refresh the index"

        # The edit an agent would make mid-task.
        (tmp_path / "mod.py").write_text("def beta():\n    return 2\n")

        assert not _found(tools.call("find_symbol", {"name": "beta"})), \
            "precondition: the index has not noticed the edit yet"

        out = json.loads(tools.call("reindex", {}))
        assert out["files_updated"] >= 1

        assert "beta" in _found(tools.call("find_symbol", {"name": "beta"}))
        assert not _found(tools.call("find_symbol", {"name": "alpha"})), \
            "the deleted symbol must disappear, not linger from the old snapshot"
    finally:
        store.close()


def test_reindex_does_not_call_a_model_by_default(tmp_path):
    """An MCP tool call must not silently spend model tokens.

    Indexing *can* generate LLM file purposes; the things an edit invalidates (symbols,
    structure, the call map) do not need one. If this regresses, every reindex from an
    editor turns into a slow, billable run the user never asked for.
    """
    _repo(tmp_path, "def alpha():\n    return 1\n")
    settings = _settings()
    index_repo(tmp_path, settings)

    store = AffordanceStore(tmp_path, ".secagent")
    try:
        tools = build_affordance_tools(store, settings=settings)
        out = json.loads(tools.call("reindex", {}))
        assert out["llm_summaries"] is False
        assert settings.affordances.llm_summaries is False
    finally:
        store.close()


def test_reopen_sees_another_connection_write(tmp_path):
    """The mechanism underneath: a long-lived reader must not pin a stale snapshot."""
    _repo(tmp_path, "def alpha():\n    return 1\n")
    settings = _settings()
    index_repo(tmp_path, settings)

    reader = AffordanceStore(tmp_path, ".secagent")
    try:
        assert reader.existing_sha("mod.py") is not None
        before = reader.existing_sha("mod.py")

        (tmp_path / "mod.py").write_text("def beta():\n    return 2\n")
        index_repo(tmp_path, settings)          # writes via its own connection

        reader.reopen()
        assert reader.existing_sha("mod.py") != before
    finally:
        reader.close()
