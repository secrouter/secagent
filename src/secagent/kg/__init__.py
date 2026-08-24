"""Knowledge graph: a Tier-1 push-retrieval graph over the target repo.

The design (per the "Applying Knowledge Graphs" Tier-1 pattern): code queries the
graph and pushes the answer facts into the model's context *before* the turn, so
retrieval is deterministic and constant-cost instead of the model grep-reading during
the turn. Three tables (entity exists, two entities relate, an entity has another
name), a recursive-CTE traversal, and a pre-prompt injection hook.

It is a general substrate: any domain writes the same ``kg_entities`` / ``kg_relations``
/ ``kg_aliases`` shapes via an *extractor*. The first extractor is deterministic — it
projects secagent's existing affordance graph (symbols, calls, types, io) into the KG
with no LLM at all (see :mod:`secagent.kg.project`).
"""

from __future__ import annotations

from .hashing import entity_id, normalise
from .models import Entity, Fact, Relation
from .store import KnowledgeGraph

__all__ = [
    "Entity",
    "Fact",
    "KnowledgeGraph",
    "Relation",
    "entity_id",
    "normalise",
]
