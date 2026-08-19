"""Light-vs-heavy call-extractor registry: one place that selects, per language, the
fast syntactic extractor or the (optional, slower) semantic one at KG build time.

Extractor contract
-------------------
An extractor is any callable ``(kg: KnowledgeGraph, store: AffordanceStore) -> int``.
It walks ``store``'s files for its language(s), and for each resolved call:

  - ``cid = kg.add_entity(caller_name, "SYMBOL", qualifier=<call-site file>)``
  - ``did = kg.add_entity(callee_name, "SYMBOL", qualifier=<defining file>)``
  - if ``cid != did`` (drop self-loops — recursion or a namesake resolving to itself):
    ``kg.add_relation(cid, did, "calls", source=<call-site file>)`` and count it.

It calls ``kg.commit()`` itself before returning (mirroring ``pycalls``/
``treesitter_calls``) and returns the number of edges it added.

Light vs. heavy
----------------
LIGHT extractors resolve a call by NAME against the repo's own definitions — fast, no
type inference, and unable to dispatch ``obj.method()`` to the receiver's actual type.
HEAVY extractors resolve semantically (type inference / a language server / a compiler
frontend) and so can resolve ``obj.method()`` correctly, at the cost of being slower
and requiring an extra runtime dependency that may not be installed.

A heavy extractor lives in a module named, by FIXED CONVENTION, under ``secagent.kg``:

  language     module                    exports
  ----------   ------------------------  --------------------------------------------
  python       ``secagent.kg.pyjedi``    ``extract_python_calls_jedi``,
                                          ``python_semantic_available() -> bool``
  go           ``secagent.kg.gocalls_semantic``
                                          ``extract_go_semantic``,
                                          ``go_semantic_available() -> bool``
  typescript,  ``secagent.kg.tscalls_semantic``
  javascript                             ``extract_ts_semantic``,
                                          ``ts_semantic_available() -> bool``

Each heavy module MUST export both the extractor function and a zero-argument
``..._available() -> bool`` predicate — True only when the module's runtime
dependency is actually usable (importable AND working), not merely importable in
principle. A module that does not exist yet (not yet built by another agent) is a
clean no-op here: the loader below wraps the import in ``try/except ImportError`` and
returns ``None``, so ``deep=True`` silently falls back to the light extractor for that
language until the heavy module lands. Landing the module requires NO change to this
file — it is picked up purely by the naming convention above.

Selection
---------
``run_extractors(kg, store, deep=...)`` runs, for every language present in the repo,
the heavy extractor when ``deep`` is on and its ``available()`` is True, else the
light one — and skips a language entirely when an affordance HEAVY backend
(clang/csharp/rust/...) already wrote resolved ``calls`` edges for it in the
affordance store (``project_affordances`` already projected those; see
``_affordance_resolved_languages``), so a name-based light pass never adds
lower-quality duplicates alongside them.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import TYPE_CHECKING, NamedTuple

from .pycalls import extract_python_calls
from .treesitter_calls import extract_treesitter_calls

if TYPE_CHECKING:
    from ..affordances.store import AffordanceStore
    from .store import KnowledgeGraph

# (kg, store) -> edge count.
Extractor = Callable[["KnowledgeGraph", "AffordanceStore"], int]
# The heavy extractor function paired with its availability predicate.
HeavyPair = tuple[Extractor, Callable[[], bool]]
# Lazily imports the heavy module by convention; None if it doesn't exist (yet).
HeavyLoader = Callable[[], "HeavyPair | None"]


class LanguageExtractors(NamedTuple):
    light: Extractor
    heavy_loader: HeavyLoader


def _load_heavy(module: str, extract_attr: str, available_attr: str) -> HeavyPair | None:
    """Import ``secagent.kg.<module>`` by NAME (not a static ``from . import``, since a
    not-yet-added Go/TS heavy module must not be a mypy/import-time error) and pull the
    two conventional exports off it. ``None`` if the module doesn't exist (yet)."""
    try:
        mod = importlib.import_module(f".{module}", __package__)
    except ImportError:
        return None
    return getattr(mod, extract_attr), getattr(mod, available_attr)


def _load_python_heavy() -> HeavyPair | None:
    return _load_heavy("pyjedi", "extract_python_calls_jedi", "python_semantic_available")


def _load_go_heavy() -> HeavyPair | None:
    return _load_heavy("gocalls_semantic", "extract_go_semantic", "go_semantic_available")


def _load_ts_heavy() -> HeavyPair | None:
    return _load_heavy("tscalls_semantic", "extract_ts_semantic", "ts_semantic_available")


# language (``FileRecord.language.lower()``) -> its light/heavy extractors. Go,
# TypeScript, and JavaScript share one light callable (``extract_treesitter_calls``
# walks all three grammars in a single pass) but each gets its own heavy loader, since
# a semantic Go extractor and a semantic TS/JS extractor are different tools.
REGISTRY: dict[str, LanguageExtractors] = {
    "python": LanguageExtractors(extract_python_calls, _load_python_heavy),
    "go": LanguageExtractors(extract_treesitter_calls, _load_go_heavy),
    "typescript": LanguageExtractors(extract_treesitter_calls, _load_ts_heavy),
    "javascript": LanguageExtractors(extract_treesitter_calls, _load_ts_heavy),
}


def _affordance_resolved_languages(store: AffordanceStore) -> set[str]:
    """Languages already covered by resolved affordance ``calls`` edges.

    The clang/csharp/rust heavy AFFORDANCE backends (and the compiled-analyzer ingest
    path) resolve each call to the callee's real defining file and write it into the
    affordance store's ``calls`` table; ``project_affordances`` already projects those
    into the KG. Running a NAME-based light extractor for the same language on top
    would only add lower-quality duplicate edges, so it is skipped. Detected
    generically (no per-backend special-casing here): any language whose files appear
    as the caller side of an affordance call edge.
    """
    lang_by_path = {rec.path: rec.language.lower() for rec in store.file_records()}
    return {
        lang_by_path[edge.src_file]
        for edge in store.load_call_edges()
        if edge.src_file in lang_by_path
    }


def run_extractors(kg: KnowledgeGraph, store: AffordanceStore, *, deep: bool = False) -> int:
    """Run the selected extractor for every language present in ``store``.

    Per language: the heavy extractor when ``deep`` is True and its module is
    available, else the light one; skipped entirely when an affordance heavy backend
    already resolved that language's calls (see ``_affordance_resolved_languages``).
    A callable selected for more than one language (Go/TS/JS's shared light
    extractor) runs exactly once. Returns the total edge count.
    """
    langs = {rec.language.lower() for rec in store.file_records()} & REGISTRY.keys()
    langs -= _affordance_resolved_languages(store)

    # Go/TS/JS's shared light callable (`extract_treesitter_calls`) is special-cased:
    # it must run *once*, scoped only to the languages that actually selected it —
    # never the whole repo — so a language whose heavy extractor ran (or that was
    # skipped as affordance-resolved) doesn't also get walked "for free" via the
    # extension map and picked up lower-quality/duplicate edges. Any other extractor
    # (python's, or a future non-shared heavy one) keeps the plain id(fn) dedupe.
    ts_light_langs: set[str] = set()
    to_run: dict[int, Extractor] = {}
    for lang in sorted(langs):
        spec = REGISTRY[lang]
        fn = spec.light
        if deep:
            heavy = spec.heavy_loader()
            if heavy is not None:
                extract_fn, available = heavy
                if available():
                    fn = extract_fn
        if fn is extract_treesitter_calls:
            ts_light_langs.add(lang)
            continue
        to_run[id(fn)] = fn

    total = sum(fn(kg, store) for fn in to_run.values())
    if ts_light_langs:
        total += extract_treesitter_calls(kg, store, langs=frozenset(ts_light_langs))
    return total
