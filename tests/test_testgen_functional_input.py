"""What the functional path was given, recorded honestly, and refused when it is nothing.

Two separate things, and the first was originally mistaken for the second.

THE COUNTING BUG, which is real. `saw_chars`/`total_chars` were never set on the
functional path at all — they sat at their dataclass default of 0. So every functional
entry in the manifest read `saw_chars: 0, total_chars: 0` regardless of what the model
actually received, which is a false statement in the record and reads exactly like
"generated from nothing". They now carry the real context size.

THE GUARD, which is correct but rarely reachable. When a component has no summarised
member files and no IO edges, the prompt is a name, a language, "(no detected IO edges)"
and "(none)". Generating from that is not a partial success, so it is now recorded failed
and nothing is written. On an ordinary repo every indexed file gets a summary, so this
cannot be reached through `generate_tests` — the tests below drive `_gen_functional`
directly with the summaries dict that is its real input.

WHAT NEITHER OF THESE FIXES. On the PX4 GNSS drivers `functional/test_root.cpp` is wholly
fabricated — `GpsParser.h`, `ParseResult`, `ErrorCode::EMPTY_INPUT`, none of which exist —
and the `(root)` component it came from had summaries for all five of its files. They were
`.editorconfig` → "Other file (202 bytes).", `CMakeLists.txt` → "Other file (487 bytes).",
a licence excerpt, "GPS Drivers", and one test file. Non-empty, and worth nothing. The
model invented an API from junk context, which is a quality problem in what the functional
path is given — it is never given source, by design — not a counting bug.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from secagent.agents.testgen.agent import generate_tests
from secagent.config import Settings

from .conftest import make_chat_response, mock_client


def _settings(tmp_path) -> Settings:
    s = Settings()
    s.affordances.llm_summaries = False
    s.affordances.store_dir = str(tmp_path / "store")
    s.testgen.max_unit_files = 0          # functional path only
    s.testgen.max_functional_components = 5
    return s


def _inventing_llm():
    """A model that answers confidently no matter how little it was given — which is
    exactly what the real one did."""
    return mock_client(lambda r: httpx.Response(200, json=make_chat_response(
        content='#include "GpsParser.h"\nTEST(X, Y) { ASSERT_TRUE(true); }\n')))


def _bare_repo(tmp_path) -> Path:
    """A component with source files but no indexed purposes and no IO edges."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.c").write_text("int f(void) { return 1; }\n")
    return repo


def _components(repo, s):
    """Index the repo and hand back (store, cfg, framework_cache) for a direct call."""
    from secagent.affordances.api import index_repo
    from secagent.affordances.store import AffordanceStore

    index_repo(repo, s)
    return AffordanceStore(repo, s.affordances.store_dir)


def test_a_component_with_no_input_is_recorded_failed_not_generated(tmp_path):
    """`_gen_functional` is called with the summaries dict as an input. When a component's
    files are absent from it AND the component has no IO edges, the prompt carries the
    component's name, its language, "(no detected IO edges)" and "(none)" — nothing else.

    Exercised through the real function with that real input rather than through
    `generate_tests`, because on an ordinary repo every indexed file gets a summary, so
    the whole-pipeline route cannot reach this branch. A test that cannot fail is worse
    than no test.
    """
    from secagent.agents.testgen.agent import _gen_functional

    repo = _bare_repo(tmp_path)
    s = _settings(tmp_path)
    store = _components(repo, s)
    try:
        results, _skipped, _used = _gen_functional(
            repo, tmp_path / "out", store, {}, s.testgen, _inventing_llm(), {})
    finally:
        store.close()

    assert results, "the component must still be reported, not silently dropped"
    for t in results:
        assert t.ok is False, f"{t.path} was generated from nothing and recorded ok=True"
        assert "no input" in t.error, t.error
        assert t.saw_chars == 0
        assert not (tmp_path / "out" / t.path).exists(), (
            "a test invented from nothing must not be written to disk")


def test_ok_is_never_true_on_a_zero_length_input(tmp_path):
    """The invariant, stated once: nothing in the manifest may claim success on an empty
    input. This is the property that was violated for unit files and survived here."""
    from secagent.agents.testgen.agent import _gen_functional

    repo = _bare_repo(tmp_path)
    s = _settings(tmp_path)
    store = _components(repo, s)
    try:
        results, _skipped, _used = _gen_functional(
            repo, tmp_path / "out", store, {}, s.testgen, _inventing_llm(), {})
    finally:
        store.close()

    offenders = [t for t in results if t.ok and t.saw_chars == 0]
    assert not offenders, f"ok=True on zero input: {[t.path for t in offenders]}"


def test_a_component_with_real_context_is_still_generated(tmp_path):
    """Paired silence. Refusing on no input must not become refusing on thin input — a
    component whose files DO have purposes still gets its functional test."""
    repo = _bare_repo(tmp_path)
    s = _settings(tmp_path)
    s.affordances.llm_summaries = False
    out = tmp_path / "out"

    # Give the store a real file summary by indexing a file with extractable structure.
    (repo / "src" / "a.c").write_text(
        "/* Parses NMEA sentences from the serial port. */\n"
        "int parse_sentence(const char *s) { return 1; }\n"
        "int checksum(const char *s) { return 0; }\n")
    result = generate_tests(repo, s, out_dir=out, unit=False, llm=_inventing_llm())

    manifest = json.loads((out / "manifest.json").read_text())
    functional = [t for t in manifest["tests"] if t["kind"] == "functional"]
    generated = [t for t in functional if t["ok"]]
    assert generated, (
        "a component with indexed file purposes must still produce a functional test; "
        f"got {[(t['path'], t.get('error')) for t in functional]}")
    for t in generated:
        assert t["saw_chars"] > 0, "a generated test must record what it was given"
        assert result["coverage"]["components"]["succeeded"] >= 1


def test_a_file_that_produced_nothing_is_not_reported_as_partial(tmp_path):
    """`partial` checked only `saw_chars < total_chars` and never `ok`.

    Click's `core.py` is 147KB against a 24KB cap, so it was truncated — and then its
    generation timed out and produced no test at all. Both conditions held, so it was
    listed in the disclosure banner, the README and the manifest under "Incomplete source
    coverage", as though a partial but usable test existed. There was no test.

    A tool whose selling point is honest coverage reporting cannot describe a nonexistent
    artifact as partial. Failure is `failed`; partial means "here it is, and it saw only
    some of the source".
    """
    from secagent.agents.testgen.models import GeneratedTest

    timed_out = GeneratedTest("unit", "core.py", "Python", "pytest", "unit/test_core.py",
                              ok=False, saw_chars=24000, total_chars=147588,
                              error="LLM deadline exceeded")
    assert timed_out.partial is False, "a file that produced nothing is not partial"

    truncated_but_written = GeneratedTest(
        "unit", "core.py", "Python", "pytest", "unit/test_core.py",
        ok=True, saw_chars=24000, total_chars=147588)
    assert truncated_but_written.partial is True, "a real truncated test is still partial"

    whole = GeneratedTest("unit", "a.py", "Python", "pytest", "unit/test_a.py",
                          ok=True, saw_chars=500, total_chars=500)
    assert whole.partial is False


# --------------------------------------------------------------------------------------
# --verify: a test that tests nothing must not sit beside the ones that do
# --------------------------------------------------------------------------------------


def test_a_vacuous_test_is_quarantined_not_left_beside_the_good_ones(tmp_path, monkeypatch):
    """Deleting the artifact destroys the evidence; leaving it in `unit/` invites someone
    committing a green no-op. It moves to `quarantine/`, where the path alone says what it
    is, and its manifest entry stops claiming `ok`."""
    from secagent.agents.testgen import agent as tg
    from secagent.verify import TestOutcome

    repo = _bare_repo(tmp_path)
    out = tmp_path / "out"

    def fake_verify(repo_arg, rel, **kw):
        return TestOutcome(path=rel, language="C", verdict="vacuous",
                           reason="passes against all 3 mutants",
                           compiled=True, ran=True, passed_on_correct_code=True,
                           mutants_total=3, mutants_killed=0)

    monkeypatch.setattr("secagent.verify.verify_test", fake_verify)
    result = tg.generate_tests(repo, _settings(tmp_path), out_dir=out, unit=False,
                               llm=_inventing_llm(), verify=True)

    written = [p for p in out.rglob("*.c") if p.is_file()]
    assert written, "something must have been generated to quarantine"
    assert all("quarantine" in p.relative_to(out).as_posix() for p in written), (
        f"a vacuous test was left in place: {[p.name for p in written]}")
    assert result["verdicts"]["vacuous"] >= 1
    manifest = json.loads((out / "manifest.json").read_text())
    assert all(not t["ok"] for t in manifest["tests"] if t["kind"] == "functional")


def test_a_useful_test_is_left_where_it_was_written(tmp_path, monkeypatch):
    """Paired silence: quarantine must not become "move everything"."""
    from secagent.agents.testgen import agent as tg
    from secagent.verify import TestOutcome

    repo = _bare_repo(tmp_path)
    out = tmp_path / "out"
    monkeypatch.setattr(
        "secagent.verify.verify_test",
        lambda repo_arg, rel, **kw: TestOutcome(
            path=rel, language="C", verdict="useful", compiled=True, ran=True,
            passed_on_correct_code=True, mutants_total=3, mutants_killed=3))
    tg.generate_tests(repo, _settings(tmp_path), out_dir=out, unit=False,
                      llm=_inventing_llm(), verify=True)

    assert not (out / "quarantine").exists()
    assert list((out / "functional").glob("*.c")), "a useful test must stay put"


def test_without_verify_nothing_claims_to_have_been_checked(tmp_path):
    """Absence of a verdict must never read as "checked and clean"."""
    out = tmp_path / "out"
    result = generate_tests(_bare_repo(tmp_path), _settings(tmp_path), out_dir=out,
                            unit=False, llm=_inventing_llm())
    assert "verification" not in result
    assert "verdicts" not in result


# --------------------------------------------------------------------------------------
# Extraction and target selection — two bugs the corpus run surfaced
# --------------------------------------------------------------------------------------


def test_an_unterminated_code_fence_is_still_stripped():
    """The regex needed a CLOSING fence. When the model runs out of budget mid-file there
    isn't one, so the match failed, the fallback returned the text unchanged, and the
    opening ```cpp went to disk as line 1. The file was counted a success and failed later
    at `error: expected unqualified-id`, which reads as model quality rather than an
    extraction bug. It cost 1 of the 4 files written in the post-convention corpus run."""
    from secagent.agents.testgen.agent import _extract_code

    truncated = '```cpp\n#include <cassert>\nvoid test_a() { assert(1); }\n'
    out = _extract_code(truncated)
    assert not out.startswith("```"), f"fence leaked into the file: {out[:20]!r}"
    assert out.startswith("#include <cassert>")


def test_a_well_formed_fence_is_still_stripped():
    """Paired silence: the case that already worked must keep working."""
    from secagent.agents.testgen.agent import _extract_code

    out = _extract_code('```cpp\nint main() { return 0; }\n```\n')
    assert out.strip() == "int main() { return 0; }"


def test_unfenced_output_is_untouched():
    """And the case with no fence at all must not lose its first line."""
    from secagent.agents.testgen.agent import _extract_code

    assert _extract_code("int main() { return 0; }\n").strip() == "int main() { return 0; }"


def test_the_projects_own_tests_are_not_generation_targets(tmp_path):
    """Handed `gps-parser-test.cpp` as a target, the model emitted a near-verbatim copy —
    same 22 assertions — that fails. Damning, but for a narrower reason than it looks: it
    was asked to write a test for a test. It also burns a slot from a bounded budget.

    Driven through `generate_tests` rather than by asserting on `is_test_path` directly:
    the classifier was always correct, the bug was that target selection never consulted
    it, so a test that only checks the classifier passes with or without the fix.
    """
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "parser.c").write_text(
        "int parse(const char *s) { return 1; }\nint checksum(const char *s) { return 0; }\n")
    (repo / "gps-parser-test.c").write_text(
        "#include <assert.h>\nvoid test_a(void) { assert(1); }\n"
        "int main(void) { test_a(); return 0; }\n")

    s = _settings(tmp_path)
    s.testgen.max_unit_files = 10
    s.testgen.max_functional_components = 0
    out = tmp_path / "out"
    generate_tests(repo, s, out_dir=out, functional=False, llm=_inventing_llm())

    manifest = json.loads((out / "manifest.json").read_text())
    targets = {t["target"] for t in manifest["tests"]}
    assert "src/parser.c" in targets, "real sources must still be targets"
    assert "gps-parser-test.c" not in targets, (
        f"a test file was used as a generation target: {sorted(targets)}")


# --------------------------------------------------------------------------------------
# Failure attribution: a deadline and an empty answer are different problems
# --------------------------------------------------------------------------------------


def _raising_llm(exc):
    """A real client (so `.config` exists for provenance) whose `chat` always raises."""
    llm = mock_client(lambda r: httpx.Response(200, json=make_chat_response(content="x")))

    def boom(*a, **kw):
        raise exc

    llm.chat = boom          # type: ignore[method-assign]
    return llm


def _unit_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.c").write_text(
        "int parse(const char *s) { return 1; }\nint sum(int a, int b) { return a + b; }\n")
    return repo


def _causes(repo, tmp_path, llm, name):
    s = _settings(tmp_path)
    s.testgen.max_unit_files = 5
    s.testgen.max_functional_components = 0
    out = tmp_path / name
    generate_tests(repo, s, out_dir=out, functional=False, llm=llm)
    manifest = json.loads((out / "manifest.json").read_text())
    return {t["error"] for t in manifest["tests"] if not t["ok"]}


def test_a_timeout_and_an_empty_answer_are_reported_differently(tmp_path):
    """`_chat_code` swallowed the exception and returned "", so every failure surfaced as
    "the model returned no usable test content" — a deadline, a transport error and a
    model that looped and wrote nothing were indistinguishable in the manifest.

    That made the largest defect in UC5 unmeasurable. Only once the causes were separated
    could a temperature change be shown to eliminate empty-content failures entirely while
    leaving timeouts behind. If these two ever collapse back into one string, that result
    stops being sayable.
    """
    from secagent.llm.client import LLMError

    repo = _unit_repo(tmp_path)
    timed_out = _causes(repo, tmp_path, _raising_llm(
        LLMError("LLM deadline exceeded after 1 attempt(s): timed out")), "a")
    empty = _causes(repo, tmp_path, mock_client(
        lambda r: httpx.Response(200, json=make_chat_response(content=""))), "b")

    assert timed_out and empty
    assert timed_out != empty, f"both causes reported identically: {timed_out}"
    assert any("timed out" in c for c in timed_out), timed_out
    assert not any("timed out" in c for c in empty), empty
    # And the empty case names emptiness rather than falling back to the generic
    # "no usable test content", which is what every failure used to say.
    assert any("empty content" in c for c in empty), empty


def test_a_transport_failure_names_its_exception_class(tmp_path):
    """Neither a timeout nor an empty answer: a third cause, and it must say so rather
    than borrow either label."""
    repo = _unit_repo(tmp_path)
    causes = _causes(repo, tmp_path, _raising_llm(ConnectionRefusedError("no route")), "c")

    assert any("ConnectionRefusedError" in c for c in causes), causes
    assert not any("timed out" in c for c in causes), causes
