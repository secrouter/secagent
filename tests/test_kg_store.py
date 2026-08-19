"""Tests for the knowledge-graph store: computed identity, merge-on-re-extract,
seed + recursive walk, and at-rest hardening in the shared index.db."""

from __future__ import annotations

import os
import sqlite3
import stat

from secagent.kg import KnowledgeGraph, entity_id, normalise


def test_entity_id_is_deterministic_and_merges_by_normalised_name():
    a = entity_id("ROLE", "Ops Manager")
    b = entity_id("ROLE", "ops  manager")  # different case + spacing
    assert a == b, "identity must be case/whitespace-insensitive so re-extraction merges"


def test_entity_id_separates_by_type():
    assert entity_id("PERSON", "Sarah") != entity_id("ROLE", "Sarah")


def test_normalise_preserves_code_identifier_shape():
    # Underscores/colons carry meaning in code identifiers; don't collapse them.
    assert normalise("Http::Client") == "http::client"
    assert normalise("  foo_bar ") == "foo_bar"


def test_add_and_count_and_rerun_merges(tmp_path):
    with KnowledgeGraph(tmp_path) as kg:
        first = kg.add_entity("Ops Manager", "ROLE", description="signs refunds")
        # Re-extract the same entity (different spelling, richer description).
        second = kg.add_entity("ops manager", "ROLE", description="")
        kg.commit()
        assert first == second
        counts = kg.counts()
        assert counts["entities"] == 1, "same (type, name) must be one node"
        # A blank re-extraction must not erase the populated description.
        assert kg.get_entity(first).description == "signs refunds"


def test_source_is_definition_first_not_last_writer(tmp_path):
    with KnowledgeGraph(tmp_path) as kg:
        eid = kg.add_entity("foo", "SYMBOL", source="def.py")  # projector: the definition
        kg.add_entity("foo", "SYMBOL", source="caller.py")  # later call-edge write
        kg.commit()
        assert kg.get_entity(eid).source == "def.py", "defining file must not be overwritten"


def test_relations_and_aliases_dedup(tmp_path):
    with KnowledgeGraph(tmp_path) as kg:
        a = kg.add_entity("Refund approvals", "POLICY")
        b = kg.add_entity("Ops Manager", "ROLE")
        kg.add_relation(a, b, "approved_by")
        kg.add_relation(a, b, "approved_by")  # duplicate edge
        kg.add_alias(b, "OM")
        kg.add_alias(b, "OM")  # duplicate alias
        kg.commit()
        counts = kg.counts()
        assert counts["relations"] == 1
        assert counts["aliases"] == 1


def _refund_chain(kg: KnowledgeGraph) -> None:
    """The artifact's trap case: policy -> role -> person -> delegate."""
    policy = kg.add_entity("Refund approvals", "POLICY", description="Over £500 needs Ops Manager")
    role = kg.add_entity("Ops Manager", "ROLE")
    sarah = kg.add_entity("Sarah Chen", "PERSON", description="On leave 1-31 March")
    marcus = kg.add_entity("Marcus Webb", "PERSON", description="Covers Sarah in March")
    kg.add_relation(policy, role, "approved_by", source="refund-policy.md")
    kg.add_relation(role, sarah, "held_by", source="org-chart.md")
    kg.add_relation(sarah, marcus, "delegates_to", source="delegation-memo.md")
    kg.add_alias(policy, "refund")
    # A decoy entity, disconnected from the chain.
    kg.add_entity("Expenses policy", "POLICY", description="unrelated £500 mention")
    kg.commit()


def test_seed_matches_name_and_alias(tmp_path):
    with KnowledgeGraph(tmp_path) as kg:
        _refund_chain(kg)
        # Alias hit ("refund" -> Refund approvals) and a name hit.
        seeds = kg.seed(["refund", "marcus webb"])
        names = {kg.get_entity(s).name for s in seeds}
        assert "Refund approvals" in names
        assert "Marcus Webb" in names


def test_walk_reaches_full_chain_and_ranks_by_depth(tmp_path):
    with KnowledgeGraph(tmp_path) as kg:
        _refund_chain(kg)
        seeds = kg.seed(["refund"])  # start at the policy only
        reached = kg.walk(seeds, hops=3)
        # policy(0) -> role(1) -> sarah(2) -> marcus(3)
        depths = {kg.get_entity(i).name: d for i, d in reached.items()}
        assert depths["Refund approvals"] == 0
        assert depths["Ops Manager"] == 1
        assert depths["Sarah Chen"] == 2
        assert depths["Marcus Webb"] == 3
        assert "Expenses policy" not in depths, "disconnected decoy must not be reached"


def test_walk_respects_hop_bound(tmp_path):
    with KnowledgeGraph(tmp_path) as kg:
        _refund_chain(kg)
        seeds = kg.seed(["refund"])
        reached = kg.walk(seeds, hops=1)
        names = {kg.get_entity(i).name for i in reached}
        assert names == {"Refund approvals", "Ops Manager"}


def test_facts_within_returns_chain_triples(tmp_path):
    with KnowledgeGraph(tmp_path) as kg:
        _refund_chain(kg)
        reached = kg.walk(kg.seed(["refund"]), hops=3)
        facts = kg.facts_within(reached)
        preds = {(f.subject, f.predicate, f.obj) for f in facts}
        assert ("Refund approvals", "approved_by", "Ops Manager") in preds
        assert ("Ops Manager", "held_by", "Sarah Chen") in preds
        assert ("Sarah Chen", "delegates_to", "Marcus Webb") in preds


def test_tables_share_index_db_without_touching_affordances(tmp_path):
    kg = KnowledgeGraph(tmp_path)
    kg.add_entity("X", "THING")
    kg.commit()
    db_path = tmp_path / ".secagent" / "index.db"
    assert db_path.exists()
    kg.close()
    # The KG tables are kg_-prefixed and coexist with any affordance tables.
    con = sqlite3.connect(db_path)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    assert {"kg_entities", "kg_relations", "kg_aliases"} <= tables


def test_index_db_is_owner_only(tmp_path):
    kg = KnowledgeGraph(tmp_path)
    kg.add_entity("X", "THING")
    kg.commit()
    kg.close()
    db_path = tmp_path / ".secagent" / "index.db"
    assert stat.S_IMODE(os.stat(db_path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(tmp_path / ".secagent").st_mode) == 0o700


def test_clear_removes_only_kg_rows(tmp_path):
    with KnowledgeGraph(tmp_path) as kg:
        _refund_chain(kg)
        kg.clear()
        kg.commit()
        assert kg.counts() == {"entities": 0, "relations": 0, "aliases": 0}
