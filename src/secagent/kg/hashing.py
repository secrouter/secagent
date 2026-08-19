"""Computed identity for graph entities.

The headline of the design: identity is computed, never looked up. An entity's id is
a hash of its type and normalised name, so the same thing named in two places becomes
one node automatically — no matching service, no ML, and re-extraction merges instead
of duplicating.
"""

from __future__ import annotations

import re
import uuid

_WHITESPACE = re.compile(r"\s+")


def normalise(name: str) -> str:
    """Canonical form used for identity: lower-cased, whitespace-collapsed, stripped.

    Two spellings that differ only in case or internal spacing ("Ops Manager" vs
    "ops  manager") must resolve to the same entity, so they must normalise identically.
    Deliberately conservative — it does not stem, transliterate, or strip punctuation,
    because for code identifiers (``foo_bar``, ``Http::Client``) those characters carry
    meaning and collapsing them would merge distinct symbols.
    """
    return _WHITESPACE.sub(" ", name.strip()).lower()


def entity_id(type_: str, name: str, qualifier: str = "") -> str:
    """Deterministic id for an entity of ``type_`` named ``name``.

    ``uuid5`` over ``"{type}:{qualifier}:{normalised name}"`` — stable across processes and
    runs, so a projector re-run or a second extractor naming the same entity writes the
    same id and the row merges. Type is part of the key so a PERSON and a ROLE that share a
    name stay distinct.

    ``qualifier`` is the disambiguating context — for a code symbol, the file it is defined
    in — so two different functions named ``_walk`` in different files are two nodes, not
    one. It is used verbatim (paths are case-significant); an empty qualifier reproduces the
    unqualified key, so name-unique entities (files by path, externals) need not pass one.
    A call to such a symbol must resolve the callee to its defining file and pass the same
    qualifier, or it lands on a different (unqualified or wrongly-qualified) node.
    """
    key = f"{type_}:{qualifier}:{normalise(name)}" if qualifier else f"{type_}:{normalise(name)}"
    return str(uuid.uuid5(uuid.NAMESPACE_OID, key))
