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
    """Lower-cased, whitespace-collapsed, stripped — for case-INSENSITIVE text matching
    (seed-term comparison, prose entity dedup).

    Deliberately conservative — it does not stem, transliterate, or strip punctuation,
    because for code identifiers (``foo_bar``, ``Http::Client``) those characters carry
    meaning and collapsing them would merge distinct symbols. NOTE it is NOT used for
    entity identity — see ``entity_id`` on why code identity must stay case-sensitive.
    """
    return _WHITESPACE.sub(" ", name.strip()).lower()


def _canonical(text: str) -> str:
    """Whitespace-collapsed and stripped, but CASE-PRESERVED — the identity form."""
    return _WHITESPACE.sub(" ", text.strip())


def entity_id(type_: str, name: str, qualifier: str = "") -> str:
    """Deterministic id for an entity of ``type_`` named ``name``.

    ``uuid5`` over ``"{type}:{qualifier}:{name}"`` (whitespace-collapsed, CASE-PRESERVED) —
    stable across processes and runs, so a projector re-run or a second extractor naming the
    same entity writes the same id and the row merges. Type is part of the key so a PERSON
    and a ROLE that share a name stay distinct.

    Identity is case-SENSITIVE: in Go (and others) case distinguishes an exported ``Handle``
    from an unexported ``handle`` in the same file, so lower-casing the name would wrongly
    merge two different functions. Prose extractors that WANT case-insensitive merging
    should lower-case the name themselves before calling this.

    ``qualifier`` is the disambiguating context — for a code symbol, the file it is defined
    in — so two different functions named ``_walk`` in different files are two nodes, not
    one. An empty qualifier reproduces the unqualified key, so name-unique entities (files
    by path, externals) need not pass one. A call to such a symbol must resolve the callee
    to its defining file and pass the same qualifier, or it lands on a different node.
    """
    canon = _canonical(name)
    key = f"{type_}:{qualifier}:{canon}" if qualifier else f"{type_}:{canon}"
    return str(uuid.uuid5(uuid.NAMESPACE_OID, key))
