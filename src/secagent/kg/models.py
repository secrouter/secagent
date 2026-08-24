"""Dataclass models for the knowledge graph.

Plain dataclasses (matching ``affordances/models.py``) — JSON-friendly, dependency-light
in the read hot path. The three shapes the store persists, plus ``Fact``, the flattened
triple the traversal hands back for formatting.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class Entity:
    """A thing that exists. ``id`` is computed from ``type`` + ``name`` (see hashing)."""

    id: str
    name: str
    type: str
    description: str = ""  # conditions (amounts, dates, windows, signatures) live here
    source: str = ""  # provenance: where this fact was extracted from

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Relation:
    """A directed, typed connection between two entities."""

    source_id: str
    target_id: str
    predicate: str
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Fact:
    """A flattened triple as traversal returns it: ``subject -[predicate]-> object``.

    Carries the endpoint names (not just ids), each endpoint's defining file, and the
    shallower traversal depth of its two endpoints — so recall can rank by nearness and by
    whether the relationship crosses a file boundary, cap to a budget, and disambiguate
    same-named endpoints by file.
    """

    subject: str
    predicate: str
    obj: str
    source: str = ""
    depth: int = 0  # min traversal depth of the nearer endpoint (0 = a seed itself)
    subject_src: str = ""  # defining file of the subject entity
    obj_src: str = ""  # defining file of the object entity

    def is_cross_file(self) -> bool:
        """Whether the two endpoints are defined in different files — a boundary-crossing
        relationship, which for "what calls X"/impact questions is the higher-signal kind."""
        return bool(self.subject_src) and bool(self.obj_src) and self.subject_src != self.obj_src

    def as_line(self) -> str:
        arrow = f"{self.subject} --[{self.predicate}]--> {self.obj}"
        return f"{arrow}  ({self.source})" if self.source else arrow

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
