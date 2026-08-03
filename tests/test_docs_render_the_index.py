"""The docs renderer must surface what the index already knows.

Every test here builds real docs and asserts on the generated ``.rst``. That is the whole
point: the type hierarchy and the PEP 562 re-exports were fixed in the store, verified
against the store by three separate reviewers, and accepted — while the rendered site
showed flat classes and no re-exports at all. A fix verified at the layer you changed is
not verified. If one of these tests can pass while ``api.rst`` is unchanged, it is the
wrong test.

The fixture is a vendored subset of real Click, which independently exhibits all four
defects: `UsageError(ClickException)` inheritance, `BaseCommand`/`MultiCommand`/
`OptionParser` PEP 562 re-exports, and `@t.overload` groups on `command`/`group`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from secagent.affordances.clang_ast import clang_available
from secagent.agents.docs.agent import build_docs
from secagent.config import Settings

CLICK = Path(__file__).parent / "fixtures" / "click"


@pytest.fixture
def settings(tmp_path) -> Settings:
    s = Settings()
    s.affordances.llm_summaries = False  # heuristic prose, no network
    s.affordances.store_dir = str(tmp_path / "store")
    return s


def _pages(repo: Path, settings: Settings, out: Path) -> dict[str, str]:
    """Build the docs and return the rendered pages by filename."""
    report = build_docs(repo, out, settings, run_sphinx=False)
    src = Path(report["write"]["source_dir"])
    return {p.name: p.read_text() for p in src.glob("*.rst")}


def _line_for(page: str, needle: str) -> str:
    for line in page.splitlines():
        if needle in line:
            return line
    raise AssertionError(f"no line containing {needle!r} in:\n{page[:2000]}")


def _component_line(page: str, name: str) -> str:
    """The "The ``x`` component groups N <Lang> file(s)." line, whose backticks are
    RST-escaped in the rendered page."""
    for line in page.splitlines():
        if "component groups" in line and name in line:
            return line
    raise AssertionError(f"no component summary for {name!r} in:\n{page[:2000]}")


# --------------------------------------------------------------------------------------
# 1. The type hierarchy exists in the store and was never rendered.
# --------------------------------------------------------------------------------------


def test_the_api_reference_shows_class_inheritance(settings, tmp_path):
    """`store.load_types()` records 22 types with their bases for this fixture, and
    `agents/docs/` contained zero calls to it — no reference to `bases`, `load_types` or
    `TypeRecord` anywhere in the docs pipeline. A reader of the generated site saw
    exactly what the original defect report described: flat, unrelated classes."""
    api = _pages(CLICK, settings, tmp_path / "docs")["api.rst"]

    assert "UsageError(ClickException)" in api, _line_for(api, "UsageError")
    assert "BadParameter(UsageError)" in api
    assert "FileError(ClickException)" in api


def test_a_class_with_no_base_is_not_annotated(settings, tmp_path):
    """Paired silence. `CliRunner` and `EchoingStdin` have `bases == []`; inventing an
    empty `()` on them would be new noise on every root class in the codebase."""
    api = _pages(CLICK, settings, tmp_path / "docs")["api.rst"]

    assert "class CliRunner``" in api, _line_for(api, "CliRunner")
    assert "CliRunner()" not in api
    assert "EchoingStdin()" not in api


# --------------------------------------------------------------------------------------
# 2. `kind="export"` was dropped by the API-reference whitelist.
# --------------------------------------------------------------------------------------


def test_pep562_reexports_appear_in_the_api_reference(settings, tmp_path):
    """The filter was `s.kind in ("function", "method", "class")`, so the `export` kind
    the indexer had just learned to produce never reached the page. The result was worse
    than the original bug: only the private `_BaseCommand` implementations were listed, so
    the site implied Click had no public `BaseCommand` at all — the name a user actually
    writes in `from click import BaseCommand`."""
    api = _pages(CLICK, settings, tmp_path / "docs")["api.rst"]

    for name in ("BaseCommand", "MultiCommand", "OptionParser"):
        assert name in api, f"PEP 562 re-export {name} missing from api.rst"


def test_constants_stay_out_of_the_api_reference(settings, tmp_path):
    """Paired silence, and a judgement worth recording rather than repeating.

    `export` was not the only kind that whitelist excluded — `constant` was too. Measured
    before deciding: on this fixture constants are 16 of 454 symbols, but on the real C++
    GPS driver target they are **378 of 591**, mostly `#define` include guards and magic
    numbers. Including them would spend two thirds of the page budget before a single
    function was listed, and the round-robin cap would push out the functions and classes
    the page is read for. They stay out deliberately, not by oversight.
    """
    api = _pages(CLICK, settings, tmp_path / "docs")["api.rst"]

    # Real `kind="constant"` symbols in this fixture. (`__version__` is NOT one — it is a
    # PEP 562 re-export, which the test above requires to be present.)
    for name in ("ui_filename", "possibilities", "show_color"):
        assert f"* ``{name}``" not in api, f"constant {name} leaked into the reference"


# --------------------------------------------------------------------------------------
# 3. `@overload` groups pad the module-purpose line.
# --------------------------------------------------------------------------------------


def test_module_purpose_does_not_repeat_overloaded_names(settings, tmp_path):
    """`_top_level_definitions` collected every top-level `FunctionDef` by name without
    collapsing `@t.overload` stub groups, so the 6-name cap was spent on duplicates. Real
    Click's `termui.py` read "defining hidden_prompt_func, prompt, prompt, prompt, confirm
    and get_pager_file" — three of six slots on one name, omitting `style`, `secho`,
    `edit` and `launch`. The separate symbol-extraction path that feeds `api.rst` was
    fixed for exactly this and this path was not.
    """
    comps = _pages(CLICK, settings, tmp_path / "docs")["components.rst"]
    line = _line_for(comps, "``decorators.py``")

    assert line.count("command") == 1, f"overload stubs still padding the line: {line}"
    assert "group" in line, "the name displaced by the duplicates must come back"


def test_a_genuinely_repeated_name_is_not_invented(settings, tmp_path):
    """Paired silence: deduplication must not start merging distinct definitions. Every
    name in the line is a real top-level definition in the file."""
    import ast

    comps = _pages(CLICK, settings, tmp_path / "docs")["components.rst"]
    line = _line_for(comps, "``decorators.py``")

    tree = ast.parse((CLICK / "decorators.py").read_text())
    real = {n.name for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    listed = line.split("defining", 1)[-1].replace(" and ", ", ")
    named = [w.strip(" .,`") for w in listed.split(",")]
    for name in (n for n in named if n):
        assert name.replace("\\", "") in real, f"{name!r} is not defined in decorators.py"


# --------------------------------------------------------------------------------------
# 4. The component-language vote: any code file outvotes every non-code file.
# --------------------------------------------------------------------------------------


def _mixed_repo(tmp_path: Path) -> Path:
    """A documentation directory with one config file, and a code directory with many
    data files — the two cases the vote has to tell apart."""
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    for i in range(37):
        (repo / "docs" / f"page{i:02d}.md").write_text(f"# Page {i}\n\nProse.\n")
    (repo / "docs" / "conf.py").write_text("project = 'x'\nextensions = []\n")

    (repo / "apps").mkdir()
    for i in range(20):
        (repo / "apps" / f"mod{i:02d}.c").write_text(f"int f{i}(void) {{ return {i}; }}\n")
    for i in range(60):
        (repo / "apps" / f"table{i:02d}.json").write_text('{"a": 1}\n')
    return repo


def test_a_documentation_directory_is_not_called_python(settings, tmp_path):
    """`docs/` was labelled "38 Python file(s)" because one `conf.py` outvoted 37 Markdown
    files: the vote took `max()` over code languages whenever *any* code file was present,
    with no ratio check at all. That renders as an authoritative, specific statistic and
    it is false."""
    comps = _pages(_mixed_repo(tmp_path), settings, tmp_path / "docs")["components.rst"]
    line = _component_line(comps, "docs")

    assert "Python" not in line, line
    assert "Markdown" in line, line


def test_a_code_directory_full_of_data_files_is_still_code(settings, tmp_path):
    """Paired silence, and the reason the original rule existed. cFS `apps/fm` is 67 C
    and header files against 128 JSON tables; a plain file-count vote binned it as JSON
    and handed it the wrong per-language toolchain. Fixing `docs/` must not undo that —
    20 C files against 60 JSON is still a C component."""
    comps = _pages(_mixed_repo(tmp_path), settings, tmp_path / "docs")["components.rst"]
    line = _component_line(comps, "apps")

    assert "C file(s)" in line or "C++" in line, line
    assert "JSON" not in line, line


# --------------------------------------------------------------------------------------
# 5. A comment fragment rendered as an API symbol, and described as one.
# --------------------------------------------------------------------------------------


_SBF_HEADER = """\
#ifndef SBF_H
#define SBF_H

typedef struct {
    uint8_t mode;      /**< Bit 0-3: type of PVT solution
                            Bit 3: set if orbit accuracy information is used(UERE/SISA)
                            Bit 4: reserved */
    uint8_t error;
} PVTGeodetic_t;

int sbf_decode(const uint8_t *buf, int len);

#endif
"""


def _cpp_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "cpp"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "sbf.h").write_text(_SBF_HEADER)
    (repo / "src" / "sbf.cpp").write_text(
        '#include "sbf.h"\nint sbf_decode(const uint8_t *buf, int len) { return len; }\n')
    return repo


def test_a_comment_fragment_is_not_listed_as_an_api_symbol(settings, tmp_path):
    """The cleanest "authoritative but false" artifact in the whole programme.

    `sbf.h:178` carries a Doxygen fragment — "Bit 3: set if orbit accuracy information is
    used(UERE/SISA)" — on its own line inside a `/**< ... */` block. The line-based regex
    fallback has no idea it is inside a comment, so the function pattern matched it (name
    `used`) and it was rendered in `api.rst` in exactly the format of a real entry, code
    literal and `(src/sbf.h:178)` citation included. In the LLM run the model then wrote
    it a fluent, confident description. The model invented nothing; it faithfully
    described text that was never a symbol.
    """
    api = _pages(_cpp_repo(tmp_path), settings, tmp_path / "docs")["api.rst"]

    assert "orbit accuracy" not in api, _line_for(api, "orbit accuracy")
    assert "``used``" not in api and "used(UERE" not in api


def test_real_declarations_next_to_comments_are_still_found(settings, tmp_path):
    """Paired silence. Ignoring comment text must not cost the declarations around it —
    a symbol extractor that goes quiet is the failure this project cares about most."""
    api = _pages(_cpp_repo(tmp_path), settings, tmp_path / "docs")["api.rst"]

    assert "sbf_decode" in api, "the real function must survive"
    assert "PVTGeodetic_t" in api, "the real typedef must survive"


# --------------------------------------------------------------------------------------
# 6. A degraded clang parse, invisible on the page.
# --------------------------------------------------------------------------------------


@pytest.mark.skipif(not clang_available(), reason="libclang not installed")
def test_the_api_reference_says_when_the_parse_was_degraded(settings, tmp_path):
    """`ParsedUnit` tracks `missing_project_headers` precisely so we can say so, and the
    docs never asked. This repo does not ship `definitions.h` by design — the parent
    project supplies it — so `sensor_gps_s *gps_position` renders as `int * gps_position`
    on every one of the seven driver constructors: the most important parameter of the
    most important method of every class in the library, stated as fact with a
    `file:line` citation and no caveat anywhere on the page.
    """
    repo = _cpp_repo(tmp_path)
    # A project header this repo does not ship — exactly the PX4 arrangement, where
    # `definitions.h` comes from PX4-Autopilot / QGroundControl. The degradation is real,
    # not recorded: clang reports the header missing and the page must say so.
    (repo / "src" / "sbf.cpp").write_text(
        '#include "definitions.h"\n#include "sbf.h"\n'
        "int sbf_decode(const uint8_t *buf, int len) { return len; }\n")
    api = _pages(repo, settings, tmp_path / "docs")["api.rst"]

    assert ".. warning::" in api, api[:900]
    assert "definitions.h" in api, "the missing header must be named"
    assert "int" in api and "may be wrong" in api


def test_a_clean_parse_adds_no_caveat(settings, tmp_path):
    """Paired silence: a warning that always fires is one nobody reads. This fixture is
    missing only `stdint.h`, a system header — verified on cFS not to degrade extraction,
    and near-universal because a pip-installed libclang ships no libc headers."""
    api = _pages(_cpp_repo(tmp_path), settings, tmp_path / "docs")["api.rst"]

    assert "definitions.h" not in api
    assert ".. warning::" not in api


# --------------------------------------------------------------------------------------
# 7. The Overview anchored the whole project on a test executable.
# --------------------------------------------------------------------------------------


def test_a_root_level_test_executable_is_not_the_project_entrypoint(settings, tmp_path):
    """The first page a newcomer reads opened with "It features a `gps-parser-test.cpp`
    entrypoint, indicating functionality related to GPS parsing." That is the entire
    framing of what the project is, and it is wrong: this is PX4's and QGroundControl's
    shared multi-protocol GPS driver library, consumed as a submodule. The correct framing
    was one page away on Components, taken from the README the tool had already read.

    Same root cause as the budget-ordering defect: `is_test_path`'s regex required an
    underscore, so a hyphenated root-level test file passed the entrypoint filter that
    exists precisely to keep test doubles out of the headline.
    """
    from secagent.affordances.call_map import is_test_path
    from secagent.affordances.project_map import entrypoint_files_from_symbols

    assert is_test_path("gps-parser-test.cpp")
    assert entrypoint_files_from_symbols([("main", "gps-parser-test.cpp")]) == set()


def test_a_real_entrypoint_is_still_found(settings, tmp_path):
    """Paired silence: the filter must not swallow genuine entrypoints. A repo whose only
    `main` is in a real source file still reports it."""
    from secagent.affordances.call_map import is_test_path
    from secagent.affordances.project_map import entrypoint_files_from_symbols

    assert not is_test_path("src/gps_parser.cpp")
    assert entrypoint_files_from_symbols([("main", "src/gps_parser.cpp")]) \
        == {"src/gps_parser.cpp"}
