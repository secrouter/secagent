"""Tests for KG recall: seeding, ranked walking, text formatting, honest empties.

Uses a synthetic graph (the artifact's refund trap) so the behaviour is exact and
independent of any extractor."""

from __future__ import annotations

from secagent.kg import KnowledgeGraph
from secagent.kg.recall import _seed_terms, recall


def _refund_graph(kg: KnowledgeGraph) -> None:
    policy = kg.add_entity("Refund approvals", "POLICY", description="Over £500 needs Ops Manager")
    role = kg.add_entity("Ops Manager", "ROLE")
    sarah = kg.add_entity("Sarah Chen", "PERSON", description="On leave 1-31 March")
    marcus = kg.add_entity("Marcus Webb", "PERSON", description="Covers Sarah's sign-offs in March")
    kg.add_relation(policy, role, "approved_by", source="refund-policy.md")
    kg.add_relation(role, sarah, "held_by", source="org-chart.md")
    kg.add_relation(sarah, marcus, "delegates_to", source="delegation-memo.md")
    kg.add_alias(policy, "refund")
    kg.add_entity("Expenses policy", "POLICY", description="unrelated £500 decoy")
    kg.commit()


def test_seed_terms_extracts_words_and_phrases_drops_stopwords():
    terms = set(_seed_terms("Who signs off an £800 refund in March"))
    assert "refund" in terms  # a token
    assert "800 refund" in terms or "refund in" in terms  # a phrase n-gram
    assert "who" not in terms and "in" not in terms  # stopwords dropped


def test_recall_walks_the_trap_chain(tmp_path):
    with KnowledgeGraph(tmp_path) as kg:
        _refund_graph(kg)
        r = recall(kg, "A customer wants an £800 refund in March. Who signs it off?")
        triples = {(f.subject, f.predicate, f.obj) for f in r.facts}
        assert ("Refund approvals", "approved_by", "Ops Manager") in triples
        assert ("Ops Manager", "held_by", "Sarah Chen") in triples
        assert ("Sarah Chen", "delegates_to", "Marcus Webb") in triples
        # The decoy shares no edge with the chain, so it never appears.
        assert all("Expenses" not in f.subject and "Expenses" not in f.obj for f in r.facts)


def test_recall_text_has_counts_line_and_notes(tmp_path):
    with KnowledgeGraph(tmp_path) as kg:
        _refund_graph(kg)
        text = recall(kg, "who signs off a refund").as_text()
        first, *_ = text.split("\n")
        assert first.startswith("memory:") and "facts recalled" in first
        assert "approved_by" in text
        assert "where:" in text  # entity notes present
        assert "On leave 1-31 March" in text  # a condition surfaced from a description


def test_recall_facts_ranked_nearest_first(tmp_path):
    with KnowledgeGraph(tmp_path) as kg:
        _refund_graph(kg)
        r = recall(kg, "refund", hops=3)
        # Seeded at the policy; the first fact must be the nearest edge (depth 0).
        assert r.facts[0].depth == 0
        assert [f.depth for f in r.facts] == sorted(f.depth for f in r.facts)


def test_recall_top_k_caps_facts(tmp_path):
    with KnowledgeGraph(tmp_path) as kg:
        _refund_graph(kg)
        r = recall(kg, "refund", hops=3, top_k=2)
        assert len(r.facts) == 2


def test_seed_terms_strips_receiver_qualifiers():
    terms = set(_seed_terms("who calls Session.OfferLeg and Http::Client"))
    assert "OfferLeg" in terms  # dotted method reference tail
    assert "Client" in terms  # namespace-qualified tail
    assert "Session.OfferLeg" in terms  # the full form is still seeded too


def test_recall_resolves_qualified_method_reference(tmp_path):
    with KnowledgeGraph(tmp_path) as kg:
        f = kg.add_entity("offer.go", "FILE")
        m = kg.add_entity("OfferLeg", "SYMBOL")
        caller = kg.add_entity("handleOffer", "SYMBOL")
        kg.add_relation(m, f, "defined_in")
        kg.add_relation(caller, m, "calls")
        kg.commit()
        # The developer writes the Go-idiomatic "Session.OfferLeg"; recall must still find it.
        r = recall(kg, "what calls Session.OfferLeg?")
        assert any(fact.subject == "handleOffer" and fact.obj == "OfferLeg" for fact in r.facts)


def test_recall_prunes_same_file_neighbor_noise(tmp_path):
    with KnowledgeGraph(tmp_path) as kg:
        f = kg.add_entity("index.ts", "FILE")
        bc = kg.add_entity("broadcast", "SYMBOL")
        forced = kg.add_entity("forced", "SYMBOL")  # unrelated const in the same file
        impl = kg.add_entity("doBroadcast", "SYMBOL")
        kg.add_relation(bc, f, "defined_in")
        kg.add_relation(forced, f, "defined_in")  # forced's ONLY edge — pure co-location
        kg.add_relation(bc, impl, "calls")
        kg.commit()
        names = {(x.subject, x.predicate, x.obj) for x in recall(kg, "broadcast").facts}
        assert ("broadcast", "defined_in", "index.ts") in names  # the seed's own location kept
        assert ("broadcast", "calls", "doBroadcast") in names  # real relationship kept
        assert ("forced", "defined_in", "index.ts") not in names  # neighbor noise pruned


def test_recall_keeps_file_members_when_the_file_is_the_seed(tmp_path):
    with KnowledgeGraph(tmp_path) as kg:
        f = kg.add_entity("offer.go", "FILE")
        a = kg.add_entity("OfferLeg", "SYMBOL")
        b = kg.add_entity("drainRTCP", "SYMBOL")
        kg.add_relation(a, f, "defined_in")
        kg.add_relation(b, f, "defined_in")
        kg.commit()
        # Asking about the FILE means its members ARE the answer — don't prune them.
        r = recall(kg, "what is defined in offer.go")
        names = {(x.subject, x.predicate, x.obj) for x in r.facts}
        assert ("OfferLeg", "defined_in", "offer.go") in names
        assert ("drainRTCP", "defined_in", "offer.go") in names


def test_recall_ranks_relationships_above_defined_in(tmp_path):
    # A symbol has both a `defined_in` (its file, a hub) and a `calls` edge, both at
    # depth 0 (they touch the seed). The real relationship must rank first.
    with KnowledgeGraph(tmp_path) as kg:
        f = kg.add_entity("mod.py", "FILE")
        a = kg.add_entity("main", "SYMBOL")
        b = kg.add_entity("helper", "SYMBOL")
        kg.add_relation(a, f, "defined_in")
        kg.add_relation(a, b, "calls")
        kg.commit()
        r = recall(kg, "main")
        assert r.facts[0].predicate == "calls"
        assert any(fact.predicate == "defined_in" for fact in r.facts)  # still present


def test_recall_miss_is_honest_and_empty(tmp_path):
    with KnowledgeGraph(tmp_path) as kg:
        _refund_graph(kg)
        r = recall(kg, "quarterly kubernetes migration budget")
        assert r.is_empty()
        assert r.as_text() == "memory: no matches"
        assert r.seed_count == 0
