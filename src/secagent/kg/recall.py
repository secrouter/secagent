"""Recall: the read hot path — seed the question, walk the graph, hand back facts.

Two jobs, whatever the tier: **seed** (match the question's words against entity names
and aliases) then **walk** (collect everything connected to the seeds, hop by hop). The
result is plain text — ranked triples plus the entity notes that carry the conditions —
shaped to drop straight in front of the model. Deterministic and constant-cost: a match
costs at most ``top_k`` facts; a miss costs one honest "no matches" line.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import Entity, Fact
from .store import KnowledgeGraph

# Common words that would seed noisily (they are never entity names worth starting from).
# The second group is code-query vocabulary — the words a developer uses to *ask about*
# code ("what CALLS X", "where is the CLASS Y"). When such a word coincidentally matches a
# symbol of the same name (a `calls()` function, a `type()` method) it seeds an irrelevant
# subgraph, so these are dropped as anchors (the specific target symbol carries the query).
_STOPWORDS = frozenset([
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has", "have",
    "how", "in", "into", "is", "it", "of", "on", "or", "that", "the", "to", "was",
    "what", "when", "where", "which", "who", "why", "with", "does", "do", "can",
    "will", "would", "should", "could", "i", "you", "show", "tell", "list", "find",
    "get", "me", "my", "our", "this", "these", "those", "it's", "whats", "what's",
    # code-query vocabulary
    "call", "calls", "called", "calling", "caller", "callers", "callee", "callees",
    "function", "functions", "method", "methods", "class", "classes", "type", "types",
    "import", "imports", "imported", "return", "returns", "define", "defined", "defines",
    "definition", "uses", "used", "using", "reference", "references", "breaks", "break",
    "change", "changing", "signature", "trace", "happens", "run", "runs",
])

# Identifier-ish tokens: keep the punctuation that carries meaning in code names
# (dots, colons, slashes, underscores) so "store.py", "Http::Client", "src/x" seed.
_TOKEN = re.compile(r"[A-Za-z0-9_./:]+")
# Receiver/namespace qualifiers a method reference is written with: Session.OfferLeg,
# Http::Client, obj->method. Splitting on these lets the tail (the actual symbol name)
# seed even when the developer wrote the qualified form.
_QUALIFIER = re.compile(r"\.|::|->")


def _seed_terms(prompt: str, *, max_ngram: int = 3) -> list[str]:
    """Candidate seed terms from a prompt: each token, plus 2- and 3-word phrases.

    Phrases catch multi-word entity names ("Refund approvals"); single tokens catch code
    identifiers and aliases. A qualified reference also seeds its final segment
    ("Session.OfferLeg" -> "OfferLeg"), so method-reference phrasing resolves. Pure-stopword
    unigrams are dropped; phrases are kept as-is (a phrase is only a seed if some entity is
    actually named that).
    """
    words = _TOKEN.findall(prompt)
    terms: set[str] = set()
    for w in words:
        if w.lower() not in _STOPWORDS:
            terms.add(w)
        tail = _QUALIFIER.split(w)[-1]
        if tail and tail != w and tail.lower() not in _STOPWORDS:
            terms.add(tail)  # Session.OfferLeg -> OfferLeg, Http::Client -> Client
    for n in range(2, max_ngram + 1):
        for i in range(len(words) - n + 1):
            terms.add(" ".join(words[i : i + n]))
    return [t for t in terms if t]


@dataclass
class Recall:
    """The result of a recall: ranked facts, supporting entity notes, and provenance."""

    facts: list[Fact] = field(default_factory=list)
    notes: list[Entity] = field(default_factory=list)
    seed_count: int = 0
    hops: int = 0

    def is_empty(self) -> bool:
        return not self.facts

    def as_text(self) -> str:
        """Format for injection. The FIRST line is the counts summary (the hook surfaces
        it as a one-line status); the rest is the facts and their conditions."""
        if self.is_empty():
            return "memory: no matches"
        lines = [f"memory: {len(self.facts)} facts recalled"]
        lines += [f.as_line() for f in self.facts]
        if self.notes:
            lines.append("where:")
            # Include the defining file so same-named symbols from different files (a
            # frequent source of collision noise) can be told apart by the reader.
            for n in self.notes:
                loc = f" [{n.source}]" if n.source else ""
                lines.append(f"  {n.name}{loc}: {n.description}")
        return "\n".join(lines)


def recall(
    kg: KnowledgeGraph, prompt: str, *, hops: int = 3, top_k: int = 8, max_notes: int = 8
) -> Recall:
    """Seed on ``prompt``, walk ``hops`` deep, return the ``top_k`` nearest facts.

    Ranking is by nearness to the seeds (shallower traversal depth first) — the design's
    default; path-membership ranking is the same-design refinement for dense hubs. The
    walk itself is unbounded in breadth; only the returned fact set is capped, so a deep
    chain still completes while a hub does not blow the budget.
    """
    seeds = kg.seed(_seed_terms(prompt))
    if not seeds:
        return Recall(hops=hops)
    seed_names = {e.name for e in kg.entities(seeds)}
    reached = kg.walk(seeds, hops=hops)
    facts = kg.facts_within(reached)

    # Prune same-file-neighbor noise: a symbol reached only because it shares a file with a
    # seed (its lone edge is `defined_in` to that file) is not relevant to the question. Keep
    # a `defined_in` fact only when its symbol is a seed or takes part in a real relationship
    # (calls/inherits/imports), OR when the file itself is a seed ("what's in file X").
    relevant = set(seed_names)
    for f in facts:
        if f.predicate != "defined_in":
            relevant.add(f.subject)
            relevant.add(f.obj)
    facts = [
        f for f in facts
        if f.predicate != "defined_in" or f.subject in relevant or f.obj in seed_names
    ]

    # Nearest-first; within a depth, real relationships (calls/inherits/imports) rank
    # above `defined_in` so a symbol's file — a hub every co-located symbol hangs off —
    # doesn't bury the edges that actually answer the question. Then a stable order so
    # identical graphs recall identically.
    facts.sort(key=lambda f: (f.depth, f.predicate == "defined_in", f.subject, f.predicate, f.obj))
    facts = facts[:top_k]

    # Entity notes: descriptions for the entities that appear in the kept facts (the
    # conditions — thresholds, leave windows, signatures — that let the model reason).
    named = {f.subject for f in facts} | {f.obj for f in facts}
    seen: set[tuple[str, str]] = set()
    notes: list[Entity] = []
    for ent in sorted(kg.entities(reached), key=lambda e: e.name):
        if ent.name in named and ent.description and (ent.name, ent.description) not in seen:
            seen.add((ent.name, ent.description))
            notes.append(ent)
            if len(notes) >= max_notes:
                break
    return Recall(facts=facts, notes=notes, seed_count=len(seeds), hops=hops)
