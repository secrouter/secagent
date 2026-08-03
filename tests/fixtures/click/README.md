# API reference coverage fixture

`utils.py` and `testing.py` are copied verbatim from the real Click library
(https://github.com/pallets/click, BSD-3-Clause, Copyright 2014 Pallets),
because the defect this fixture exercises — the API Reference page silently
dropping whole modules once the page's row cap binds — was originally found by
indexing the real Click repository. `utils.py` holds `echo`, `testing.py`
holds `CliRunner`; both are the first symbol a Click user looks for, and both
were among the symbols dropped by the pre-fix selection.

`_blocker.py` is NOT Click source. It is 300 trivial one-line function stubs,
sized to exactly match `_MAX_API_ROWS` (300) in
`src/secagent/agents/docs/outline.py`. It stands in for "some other module that
happens to be large" — in the real repository that role was played by
`core.py` (147KB, 153 symbols) plus everything alphabetically before it; using
a synthetic file here keeps the fixture small while still reproducing the
exact mechanism: a component whose files are walked in order, where an early
file's symbol count alone reaches the page cap, silently starves every file
listed after it.

Placed first in a component's file list, `_blocker.py` alone exhausts the real
production cap (300) under the pre-fix file-order selection, so `utils.py` and
`testing.py` — and therefore `echo` and `CliRunner` — are wholly absent from
the rendered page. See `tests/test_docs_rendering.py` for the test that pins
this down against the pre-fix and post-fix code.

## Python-backend parity fixture (`exceptions.py`, `decorators.py`, `__init__.py`)

Also copied verbatim from the real Click library (same source, license, and
copyright as above), for `tests/test_python_parity.py`, which pins four
defects in `src/secagent/affordances/symbols.py::_python_symbols` — all
originally found by indexing the real Click repository, not by reasoning
about synthetic snippets:

- `exceptions.py` (11.8KB) — a genuine four-level exception hierarchy
  (`ClickException` -> `UsageError` -> `BadParameter` -> `MissingParameter`,
  plus `NoSuchOption`, `FileError`, `Abort`) for the class-bases-into-`types`
  fix, and a set of `t.Final[...]`-annotated (plainly lowercase-named) class
  attributes — `message`, `show_color`, `cmd`, ... — for the `Final`-constants
  fix. It also has `t.ClassVar[int]` attributes (`exit_code` on
  `ClickException`/`UsageError`) and bare-annotated ones (`ctx`, `param`,
  `param_hint`) that must NOT be captured as constants — real negative
  fixtures for the flood-avoidance decision, not synthetic ones.
- `decorators.py` (21.7KB) — 8 real `@t.overload` stubs, giving `command` and
  `group` 5 definitions each (4 stubs + 1 real implementation) pre-fix, for
  the overload-collapse fix. Also has `pass_context`/`pass_obj` (each defined
  once, no overload) as the real-source silence case, and `R`/`T`/`FC`
  (module-level `TypeVar` assignments to short ALL-CAPS-ish names) as the
  pre-existing plain-`Assign` constant case.
- `__init__.py` (5.1KB) — the real PEP 562 `__getattr__` shim serving
  `BaseCommand`, `MultiCommand`, `OptionParser`, `__version__`,
  `get_binary_stream`, `get_text_stream` via `name == "X"` comparisons, for
  the PEP-562-exports fix. `utils.py` (already vendored above) independently
  has its own module-level `__getattr__`, spelled with `name in {"X", ...}`
  (a set literal) instead — real coverage of both comparison forms the task
  asked for — and ALSO has three *class* `__getattr__` methods
  (`_LazyFile.__getattr__`, `_KeepOpenFile.__getattr__`,
  `_PacifyFlushWrapper.__getattr__`) that must NOT be mistaken for module
  exports, since they're per-instance attribute proxying, not PEP 562.

A few attack cases the task calls out have no real-source equivalent in this
package and are synthetic, sourced inline in the test with a docstring saying
so: Click ships no `@property` setter, and none of the vendored classes use
multiple inheritance (the manager verifies `Group(Command)` separately,
against the full, unvendored 147KB `core.py`).
