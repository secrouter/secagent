"""Python-backend parity: four defects in `_python_symbols`, all found by indexing the
real Click repository (see the task brief / `tests/fixtures/click/README.md` for
provenance). Each defect gets a real-source test where real Click source exercises it,
paired with a silence test proving the fix doesn't fire when it shouldn't. A few
"attack the mechanism" cases have no real-source equivalent in the vendored files
(Click ships no `@property` setter, no multiple inheritance in the vendored classes) —
those are synthetic, and say so in their docstring.

1. `@overload` groups / `@property` accessors collapsing to one symbol.
2. Class bases recorded into the `types` table (via `symbols.python_types`), and
   `store.replace_types_for_files` — the file-scoped write that must never wipe a
   heavy backend's (C#/Rust) rows the way `store.set_types` would.
3. PEP 562 module-level `__getattr__` re-exports, recorded as `kind="export"`.
4. `Final`-annotated / ALL-CAPS constants, module- and class-level.
"""

from __future__ import annotations

from pathlib import Path

from secagent.affordances.api import index_repo
from secagent.affordances.models import TypeRecord
from secagent.affordances.queries import find_symbol
from secagent.affordances.store import AffordanceStore
from secagent.affordances.symbols import extract_symbols, python_types
from secagent.config import Settings

_CLICK = Path(__file__).parent / "fixtures" / "click"


def _syms(name: str) -> list:
    text = (_CLICK / name).read_text(encoding="utf-8")
    return extract_symbols(name, text, "Python")


def _types(name: str) -> list[TypeRecord]:
    text = (_CLICK / name).read_text(encoding="utf-8")
    return python_types(name, text)


# === 1. @overload groups and @property accessors ==========================


def test_command_and_group_collapse_to_one_real_definition():
    """Real Click `decorators.py`: `command` and `group` are each defined 5 times —
    four `@t.overload` stubs (unreachable at runtime) plus the real implementation.
    Pre-fix, `decorator_list` is never read, so all 5 become separate `command`
    symbols; the API reference then lists `command` four times as a signature a
    reader cannot actually call. Line numbers below are exact, taken directly from
    the vendored file — the real implementation is last in source order, as Python
    redefinition always puts the winning def last.
    """
    syms = _syms("decorators.py")
    commands = [s for s in syms if s.name == "command" and s.parent == ""]
    groups = [s for s in syms if s.name == "group" and s.parent == ""]
    assert len(commands) == 1, f"expected exactly one `command`, got {len(commands)}"
    assert len(groups) == 1, f"expected exactly one `group`, got {len(groups)}"
    assert commands[0].lineno == 168, "must keep the real impl, not the first @overload stub"
    assert groups[0].lineno == 293, "must keep the real impl, not the first @overload stub"
    # The real implementation's signature — proof it's not an arbitrary stub kept by
    # accident; a stub's signature would show `name: _AnyCallable` with no `attrs`.
    assert "attrs" in commands[0].signature
    assert "attrs" in groups[0].signature


def test_a_function_defined_once_stays_once():
    """Silence pairing for the dedupe above, on real source: `pass_context` and
    `pass_obj` in `decorators.py` are each defined exactly once (no `@overload`
    involved at all) — the dedupe machinery must be a no-op when there's nothing to
    collapse."""
    syms = _syms("decorators.py")
    assert sum(1 for s in syms if s.name == "pass_context") == 1
    assert sum(1 for s in syms if s.name == "pass_obj") == 1


def test_echo_appears_exactly_once():
    """The regression pin the task calls out by name: this already held before the
    fix (there's no `@overload` on `echo` in real Click) and must keep holding —
    the silence half of defect 1, not the fix half."""
    syms = _syms("utils.py")
    echoes = [s for s in syms if s.name == "echo"]
    assert len(echoes) == 1
    assert echoes[0].kind == "function"


def test_overload_group_with_no_implementation_keeps_last_stub():
    """Attack the mechanism: a malformed-but-parseable module where EVERY definition
    of a name is `@overload` — no real implementation at all. Synthetic; Click's own
    `@overload` groups all have a real implementation, so this shape has no real-source
    equivalent. Per the task's rule ("if every definition is an overload, keep the
    last"), the last stub is what `getattr(module, name)` would actually resolve to
    at runtime, so that's what gets kept.
    """
    text = (
        "import typing as t\n\n"
        "@t.overload\n"
        "def f(x: int) -> int: ...\n\n"
        "@t.overload\n"
        "def f(x: str) -> str: ...\n"
    )
    syms = extract_symbols("stub_only.py", text, "Python")
    fs = [s for s in syms if s.name == "f"]
    assert len(fs) == 1
    assert fs[0].lineno == 7, "no real impl exists; must keep the LAST stub"


def test_property_setter_deleter_triple_keeps_getter():
    """Attack the mechanism: a `@property` / `@x.setter` / `@x.deleter` triple.
    Synthetic — the task brief notes Click's own package contains no `@property`
    setter, so this shape is not backed by any real Click source; it probes the
    dedupe rule directly so it cannot regress even though nothing in the vendored
    fixtures exercises it.
    """
    text = (
        "class Widget:\n"
        "    @property\n"
        "    def value(self):\n"
        "        return self._value\n\n"
        "    @value.setter\n"
        "    def value(self, v):\n"
        "        self._value = v\n\n"
        "    @value.deleter\n"
        "    def value(self):\n"
        "        del self._value\n"
    )
    syms = extract_symbols("widget.py", text, "Python")
    values = [s for s in syms if s.name == "value" and s.parent == "Widget"]
    assert len(values) == 1
    assert values[0].lineno == 3, "must keep the getter, not the setter/deleter"
    assert values[0].signature == "def value(self)"


# === 2. Class bases into the `types` table =================================


def test_click_exception_hierarchy_bases():
    """Real Click `exceptions.py`: a genuine four-level hierarchy. Pre-fix,
    `ast.ClassDef.bases` is never read at all, so `python_types` doesn't exist and
    every one of these renders as an unrelated flat class."""
    types = {t.qualified_name: t for t in _types("exceptions.py")}
    assert types["ClickException"].bases == ["Exception"]
    assert types["UsageError"].bases == ["ClickException"]
    assert types["BadParameter"].bases == ["UsageError"]
    assert types["MissingParameter"].bases == ["BadParameter"]
    assert types["NoSuchOption"].bases == ["UsageError"]
    assert types["FileError"].bases == ["ClickException"]
    assert types["Abort"].bases == ["RuntimeError"]
    for t in types.values():
        assert t.kind == "class"
        assert t.file == "exceptions.py"


def test_dotted_base_recorded_as_written():
    """Real Click `testing.py`: `BytesIOCopy(io.BytesIO)` and
    `_NamedTextIOWrapper(io.TextIOWrapper)` — the base name is recorded exactly as
    spelled in source (`io.BytesIO`), not resolved to a fully-qualified name; the
    task brief is explicit that inventing a resolution is out of scope and would be
    worse than the plain written form."""
    types = {t.qualified_name: t for t in _types("testing.py")}
    assert types["BytesIOCopy"].bases == ["io.BytesIO"]
    assert types["_NamedTextIOWrapper"].bases == ["io.TextIOWrapper"]


def test_class_with_no_bases_records_none():
    """Silence pairing, on real source: Click's `CliRunner` and `Result` (in
    `testing.py`) declare no base class at all (implicit `object`) — the type record
    must still exist (the class itself is real) but with an empty `bases` list, not a
    fabricated one."""
    types = {t.qualified_name: t for t in _types("testing.py")}
    assert types["CliRunner"].bases == []
    assert types["Result"].bases == []


def test_multiple_inheritance_records_all_bases():
    """Attack the mechanism: multiple inheritance. Synthetic — none of the vendored
    Click classes use it (the manager is verifying `Group(Command)` separately
    against the full, unvendored `core.py`). `bases` must list every base, in order,
    and a `metaclass=` keyword argument (a keyword, not a positional base) must not
    leak in as if it were one.
    """
    text = (
        "class Base1:\n    pass\n\n"
        "class Base2:\n    pass\n\n"
        "class Mixed(Base1, Base2, metaclass=type):\n    pass\n"
    )
    types = {t.qualified_name: t for t in python_types("mixed.py", text)}
    assert types["Mixed"].bases == ["Base1", "Base2"]


def test_replace_types_for_files_preserves_other_files_types(tmp_path):
    """Pins the manager's explicit decision: `replace_types_for_files` must delete
    and insert only rows for the files it was given, never touch anything else —
    unlike `set_types`, which is a whole-table replace and exists for a different
    caller (a heavy-backend ingest that owns the complete picture for one shot)."""
    store = AffordanceStore(tmp_path, ".secagent")
    try:
        # Simulate a heavy C# backend (Roslyn) having already populated `types`.
        store.set_types([TypeRecord("NS.Widget", "class", "widget.cs", 1, ["NS.Base"])])
        store.commit()

        store.replace_types_for_files(
            ["a.py"], [TypeRecord("Foo", "class", "a.py", 1, ["Bar"])])
        store.commit()

        types = {t.qualified_name: t for t in store.load_types()}
        assert "NS.Widget" in types, "unrelated file's type record must survive"
        assert types["NS.Widget"].bases == ["NS.Base"]
        assert types["Foo"].bases == ["Bar"]

        # Re-indexing a.py with a DIFFERENT class must drop the stale one, not just
        # accumulate — matching upsert_file's replace-this-file's-rows semantics.
        store.replace_types_for_files(
            ["a.py"], [TypeRecord("Baz", "class", "a.py", 1, [])])
        store.commit()
        types = {t.qualified_name: t for t in store.load_types()}
        assert "Foo" not in types
        assert "Baz" in types
        assert "NS.Widget" in types
    finally:
        store.close()


def test_python_reindex_preserves_heavy_backend_csharp_types(tmp_path):
    """End-to-end version of the test above, through `index_repo` itself: a Python
    file with a class, indexed AFTER a heavy C# backend already wrote `types` rows
    (e.g. via `secagent analyze deep`). The light Python pass must add its own class's
    type record without wiping the C# one — the exact scenario `set_types` would
    silently break (it does `DELETE FROM types` with no WHERE clause)."""
    (tmp_path / "models.py").write_text("class Foo(Bar):\n    pass\n", encoding="utf-8")
    s = Settings()
    s.affordances.llm_summaries = False
    s.affordances.store_dir = str(tmp_path / ".secagent")

    store = AffordanceStore(tmp_path, s.affordances.store_dir)
    store.set_types([TypeRecord("NS.Widget", "class", "widget.cs", 1, ["NS.Base"])])
    store.commit()
    store.close()

    index_repo(tmp_path, s, llm=None)

    store = AffordanceStore(tmp_path, s.affordances.store_dir)
    try:
        types = {t.qualified_name: t for t in store.load_types()}
        assert "NS.Widget" in types, "C# heavy-backend type must survive a Python re-index"
        assert types["NS.Widget"].bases == ["NS.Base"]
        assert "Foo" in types, "the Python file's own class must be recorded"
        assert types["Foo"].bases == ["Bar"]
    finally:
        store.close()


# === 3. PEP 562 re-exports ===================================================


def test_pep562_shim_exposes_deprecated_names():
    """Real Click `__init__.py`: a module-level `__getattr__` serving `BaseCommand`,
    `MultiCommand`, `OptionParser`, `__version__`, `get_binary_stream`,
    `get_text_stream` via `name == "X"` comparisons. Pre-fix these are invisible —
    `find_symbol BaseCommand` answers "No symbols matching", worded identically to
    "does not exist"."""
    syms = _syms("__init__.py")
    exports = {s.name for s in syms if s.kind == "export"}
    assert exports == {
        "BaseCommand", "MultiCommand", "OptionParser",
        "__version__", "get_binary_stream", "get_text_stream",
    }


def test_pep562_export_findable_via_find_symbol(tmp_path):
    """The actual consumer-facing fix: `find_symbol` must locate `BaseCommand` once
    the file is indexed — not the placeholder "No symbols matching" a model cannot
    tell apart from "this name does not exist in this codebase"."""
    (tmp_path / "__init__.py").write_text(
        (_CLICK / "__init__.py").read_text(encoding="utf-8"), encoding="utf-8")
    s = Settings()
    s.affordances.llm_summaries = False
    s.affordances.store_dir = str(tmp_path / ".secagent")
    index_repo(tmp_path, s, llm=None)
    store = AffordanceStore(tmp_path, s.affordances.store_dir)
    try:
        result = find_symbol(store, "BaseCommand")
        assert "No symbols matching" not in result
        assert "BaseCommand" in result
    finally:
        store.close()


def test_export_kind_excluded_from_function_method_class_filter():
    """Every consumer of symbol `kind` was checked before settling on `"export"`: the
    API reference page (`agents/docs/outline.py::_api_reference_page`) filters on
    `kind in ("function", "method", "class")`, so an export correctly does not appear
    there — it's an alias, not an API entry to document. This pins that as a verified
    decision: `export` symbols exist, and the exact tuple the reference page filters
    on is confirmed not to include it."""
    syms = _syms("__init__.py")
    exports = [s for s in syms if s.kind == "export"]
    assert exports, "fixture must actually produce exports for this test to mean anything"
    assert all(s.kind not in ("function", "method", "class") for s in exports)


def test_module_without_getattr_gains_no_exports():
    """Silence pairing, on real source: `exceptions.py` and `decorators.py` define no
    module-level `__getattr__` at all — extraction must not invent exports from
    nothing."""
    assert not [s for s in _syms("exceptions.py") if s.kind == "export"]
    assert not [s for s in _syms("decorators.py") if s.kind == "export"]


def test_class_method_getattr_is_not_a_pep562_export():
    """Attack the mechanism: real Click `utils.py` has THREE classes with their own
    `__getattr__` *method* (`_LazyFile`, `_KeepOpenFile`, `_PacifyFlushWrapper`) —
    completely unrelated to PEP 562 (it's per-instance attribute proxying, `self` is
    the first argument, not a name being looked up on a module). These must be
    recorded as ordinary methods, not scanned for exports, and must not inflate the
    module-level export count beyond the 7 names the real module-level `__getattr__`
    (also present in this same file) actually serves.
    """
    syms = _syms("utils.py")
    getattr_methods = [s for s in syms if s.name == "__getattr__" and s.kind == "method"]
    assert {s.parent for s in getattr_methods} == {
        "_LazyFile", "_KeepOpenFile", "_PacifyFlushWrapper"}
    exports = {s.name for s in syms if s.kind == "export"}
    assert exports == {
        "LazyFile", "KeepOpenFile", "make_default_short_help", "PacifyFlushWrapper",
        "safecall", "get_text_stream", "get_binary_stream",
    }


def test_pep562_in_set_literal_form():
    """Real Click `utils.py` spells its guard as `name in {"LazyFile", ...}` — a set
    literal, not the tuple form `__init__.py` uses. Both forms are real Click source
    in the vendored fixture; this test pins the set-literal path specifically."""
    syms = _syms("utils.py")
    exports = {s.name for s in syms if s.kind == "export"}
    assert "LazyFile" in exports
    assert "get_binary_stream" in exports


def test_not_equal_comparison_is_not_treated_as_a_positive_export():
    """Attack the mechanism / documents a deliberate scope boundary: the task asked
    for `==` and `in (...)` support and explicitly did not ask for `!=`. A `!=` guard
    is usually a negative check ("anything else, raise"), not a positive declaration
    of what's served, and treating it as one would be a guess. Synthetic — no vendored
    Click module happens to use this exact shape.
    """
    text = (
        "def __getattr__(name):\n"
        "    if name != 'Real':\n"
        "        raise AttributeError(name)\n"
        "    return 42\n"
    )
    syms = extract_symbols("guard.py", text, "Python")
    assert not [s for s in syms if s.kind == "export"]


# === 4. Final constants and class attributes =================================


def test_final_annotated_class_attrs_recorded_as_constants():
    """Real Click `exceptions.py`: `t.Final[...]`-annotated attributes, on plainly
    lowercase names (`message`, `show_color`, `cmd`, ...) — the language's own signal
    for "this doesn't change," which this task treats as sufficient on its own,
    independent of ALL-CAPS naming."""
    syms = _syms("exceptions.py")
    consts = {(s.parent, s.name) for s in syms if s.kind == "constant"}
    assert ("ClickException", "message") in consts
    assert ("ClickException", "show_color") in consts
    assert ("UsageError", "cmd") in consts
    assert ("MissingParameter", "param_type") in consts
    assert ("FileError", "filename") in consts
    assert ("FileError", "ui_filename") in consts
    assert ("Exit", "exit_code") in consts


def test_classvar_without_final_is_not_a_constant():
    """Silence pairing, on real source: `ClickException.exit_code` and
    `UsageError.exit_code` are `t.ClassVar[int]`, not `t.Final[...]` — `ClassVar`
    marks "shared across instances," not "constant," and is deliberately not treated
    as one. (Contrast `Exit.exit_code`, a *different* class, which IS `t.Final[int]`
    and IS captured — see the test above; same attribute name, different class,
    different annotation, different outcome.)"""
    syms = _syms("exceptions.py")
    consts = {(s.parent, s.name) for s in syms if s.kind == "constant"}
    assert ("ClickException", "exit_code") not in consts
    assert ("UsageError", "exit_code") not in consts


def test_bare_annotated_attribute_is_not_a_constant():
    """Silence pairing / flood-avoidance, on real source: a bare annotated class
    attribute with neither an ALL-CAPS name nor a `Final` annotation is a typed
    instance-attribute declaration (`ctx: Context | None`, `param: Parameter | None`
    in `exceptions.py`; `name: str`, `mode: str`, ... on Click's `_LazyFile` in
    `utils.py` — a real class with SEVEN such declarations), not a constant.
    Capturing every one of these was rejected deliberately: real classes commonly
    declare several, and treating each as a symbol would flood the table with what
    are really just typed fields."""
    exc_consts = {(s.parent, s.name) for s in _syms("exceptions.py") if s.kind == "constant"}
    assert ("BadParameter", "param") not in exc_consts
    assert ("BadParameter", "param_hint") not in exc_consts
    assert ("NoArgsIsHelpError", "ctx") not in exc_consts

    lazyfile_consts = {
        s.name for s in _syms("utils.py") if s.kind == "constant" and s.parent == "_LazyFile"}
    assert lazyfile_consts == set(), (
        "_LazyFile has 7 bare `name: str`-style annotated attributes; none is Final "
        "or ALL-CAPS, so none should be captured"
    )


def test_module_level_final_and_annassign_constants():
    """Attack the mechanism / defect-4 fix, module level: `ast.AnnAssign` — the
    modern `X: Final = ...` / `X: int = ...` spelling — was dropped entirely pre-fix
    (only bare `ast.Assign` was read). Synthetic: none of the vendored Click files
    happen to declare a module-level `Final`/annotated constant, so this exercises
    the mechanism directly. Four cases in one snippet: ALL-CAPS with a plain
    annotation, ALL-CAPS with `Final`, lowercase with `Final`, and lowercase with
    neither (must NOT be captured — the flood-avoidance case at module level).
    """
    text = (
        "from typing import Final\n\n"
        "MAX_RETRIES: int = 3\n"
        'API_BASE: Final = "https://example.invalid"\n'
        "timeout: Final[int] = 30\n"
        "local_var: int = 5\n"
    )
    syms = extract_symbols("config.py", text, "Python")
    consts = {s.name for s in syms if s.kind == "constant"}
    assert consts == {"MAX_RETRIES", "API_BASE", "timeout"}
    assert "local_var" not in consts


def test_existing_all_caps_assign_still_works():
    """Silence/regression pairing: the pre-existing behavior (plain `ast.Assign` to
    an ALL-CAPS name, module level) must keep working unchanged. Real Click
    `decorators.py` has `R`/`T`/`FC` (single/short uppercase `TypeVar` names, module
    level, plain `Assign` — no annotation at all)."""
    syms = _syms("decorators.py")
    consts = {s.name for s in syms if s.kind == "constant"}
    assert {"R", "T", "FC"} <= consts


# === API-reference-shape symbol counts, pinned so a future regression is loud =====


def test_api_eligible_symbol_counts_unchanged_for_the_docs_rendering_fixture():
    """`test_docs_rendering.py::test_api_reference_round_robins_symbol_budget_across_
    files` hard-codes utils.py=34 and testing.py=41 "api-eligible" (kind in
    (function, method, class)) symbols, measured against the real Click source. This
    fix must not perturb those counts — the new `export`/`constant` kinds it adds are
    outside that filter by construction — so this pins the same two numbers
    independently, from this file, so a future change to either fixture or filter
    trips a second, more specific assertion pointing at the actual cause instead of
    just the downstream row-cap arithmetic in the docs test.
    """
    def api_eligible(name: str) -> int:
        return sum(1 for s in _syms(name) if s.kind in ("function", "method", "class"))

    assert api_eligible("utils.py") == 34
    assert api_eligible("testing.py") == 41
