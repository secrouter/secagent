"""The gates must tell a useful test from a vacuous one, or they are decoration.

Measured on one real generated C++ file of 10 cases: 3 wrong, 3 vacuous, 4 useful. The
three vacuous ones compile, run and pass — every assertion sealed inside `if (result ==
GotXxx)` where the model's guessed protocol string never matches. Compile and run gates
both wave them through. Only mutation catches them.

Most of what follows needs no container: mutation generation, the verdict ladder and the
build-failure classifier are pure functions over text. The orchestration is driven with a
stub sandbox. One end-to-end test uses the real toolchain and is skipped without it — it
is the only one that proves the wiring, so it is marked rather than faked.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from secagent.verify import (
    TestOutcome,
    apply_mutant,
    count_assertions,
    mutants_for,
    verdict_for,
)
from secagent.verify.diagnose import (
    ENVIRONMENT,
    FABRICATED,
    REPAIRABLE,
    TOOLCHAIN,
    classify_build_failure,
)
from secagent.verify.harness import _dependency_closure, verify_test
from secagent.verify.sandbox import IMAGES, DockerSandbox, RunResult

# --------------------------------------------------------------------------------------
# Mutation generation — pure text, no execution
# --------------------------------------------------------------------------------------

_SRC = """\
#include "parser.h"
// if (a == b) in a comment must never be mutated
Result Parser::feed(char c) {
    if (c == '$') { return Result::None; }
    if (n_ > 0 && c == '*') { return Result::GotFix; }
    return Result::Bad;
}
"""


def test_mutants_target_code_not_comments_or_strings():
    """A mutant inside a comment does not compile away — it changes nothing, survives, and
    makes a good test look vacuous. One inside a string literal changes a message, not
    behaviour, with the same effect."""
    muts = mutants_for("p.cpp", _SRC, "C++")

    assert muts, "real comparisons must be found"
    assert all(m.line != 2 for m in muts), "the comment line was mutated"
    for m in muts:
        assert m.before != m.after


def test_a_string_literal_comparison_is_not_mutated():
    src = 'int f() { const char *s = "a == b"; return 1; }\n'
    assert mutants_for("p.c", src, "C") == []


def test_one_mutant_per_line_keeps_survivors_attributable():
    """Two changes in one mutant means a survivor cannot be attributed to either."""
    src = "int f(int a, int b) { if (a == b && a != 0) { return 1; } return 0; }\n"
    muts = mutants_for("p.c", src, "C")
    assert len([m for m in muts if m.line == 1]) == 1


def test_operators_differ_by_language_and_that_is_the_design():
    """`&&` is `and` in Python. What generalises is the shape, not the spelling."""
    c_muts = mutants_for("p.c", "int f(int a){ if (a > 0 && a < 9) return 1; return 0; }\n", "C")
    py_src = "def f(a):\n    if a > 0 and a < 9:\n        return 1\n"
    py_muts = mutants_for("p.py", py_src, "Python")
    assert c_muts and py_muts
    assert any("connective" in m.operator or "comparison" in m.operator for m in c_muts)
    assert any("connective" in m.operator or "comparison" in m.operator for m in py_muts)


def test_line_range_restricts_mutants_to_claimed_code():
    """Mutating code the test never claimed produces survivors for excellent reasons and
    slanders a good test. This is not hypothetical: the first run of this harness mutated
    every file in the PX4 repo and called the project's own test vacuous."""
    src = "int a(int x){ return x == 1; }\nint b(int x){ return x == 2; }\n"
    only_first = mutants_for("p.c", src, "C", line_range=(1, 1))
    assert [m.line for m in only_first] == [1]


def test_apply_mutant_refuses_a_line_that_moved():
    muts = mutants_for("p.cpp", _SRC, "C++")
    changed = _SRC.replace("if (c == '$')", "if (c == '@')")
    with pytest.raises(ValueError):
        apply_mutant(changed, next(m for m in muts if "'$'" in m.before))


def test_apply_mutant_changes_exactly_one_line():
    m = mutants_for("p.cpp", _SRC, "C++")[0]
    out = apply_mutant(_SRC, m)
    before, after = _SRC.splitlines(), out.splitlines()
    assert len(before) == len(after)
    assert sum(1 for a, b in zip(before, after, strict=True) if a != b) == 1


def test_count_assertions_ignores_the_word_in_a_string():
    assert count_assertions('assert(x == 1);\n') == 1
    assert count_assertions('printf("assert here");\n') == 0


# --------------------------------------------------------------------------------------
# The verdict ladder
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(("kw", "expected"), [
    ({"compiled": False, "ran": False, "passed": False, "mutants_total": 0,
      "mutants_killed": 0}, "uncompilable"),
    ({"compiled": True, "ran": False, "passed": False, "mutants_total": 0,
      "mutants_killed": 0}, "unrunnable"),
    ({"compiled": True, "ran": True, "passed": False, "mutants_total": 5,
      "mutants_killed": 5}, "wrong"),
    ({"compiled": True, "ran": True, "passed": True, "mutants_total": 0,
      "mutants_killed": 0}, "unverifiable"),
    ({"compiled": True, "ran": True, "passed": True, "mutants_total": 5,
      "mutants_killed": 0}, "vacuous"),
    ({"compiled": True, "ran": True, "passed": True, "mutants_total": 5,
      "mutants_killed": 1}, "useful"),
])
def test_verdict_ladder(kw, expected):
    assert verdict_for(**kw) == expected


def test_a_failing_baseline_is_wrong_whatever_mutation_said():
    """Mutation scores against a broken baseline are noise, so `wrong` wins even with a
    perfect kill rate. Otherwise a test that fails on correct code could be called useful."""
    assert verdict_for(compiled=True, ran=True, passed=False,
                       mutants_total=10, mutants_killed=10) == "wrong"


def test_mutation_score_excludes_mutants_that_never_built():
    """A mutant that does not compile proves nothing about the test. Counting it as
    survived would make a good test look weak."""
    o = TestOutcome(path="t", language="C++", verdict="useful",
                    mutants_total=4, mutants_killed=3, mutants_invalid=6)
    assert o.mutation_score == 0.75
    assert o.to_dict()["mutation_score"] == 0.75


def test_mutation_score_is_none_when_nothing_valid_was_built():
    assert TestOutcome(path="t", language="C", verdict="unverifiable").mutation_score is None


# --------------------------------------------------------------------------------------
# Build-failure classification — decides what a repair loop may even see
# --------------------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "src").mkdir()
    # The repo's own header asks for a file it does not ship — PX4's arrangement exactly.
    (tmp_path / "src" / "helper.h").write_text('#include "../../definitions.h"\nint h();\n')
    (tmp_path / "t.cpp").write_text('#include <gtest/gtest.h>\n#include "Made.h"\n')
    return tmp_path


def test_a_header_the_repo_itself_wants_is_an_environment_gap(repo):
    """PX4-Autopilot supplies `definitions.h`; this checkout does not vendor it. Reporting
    that as a bad test would blame the generator for a missing dependency."""
    assert classify_build_failure(
        repo, "t.cpp", "fatal error: 'definitions.h' file not found") == ENVIRONMENT


def test_a_header_only_the_test_wants_is_a_fabrication(repo):
    """`GpsParser.h` existed nowhere — the model invented the component."""
    assert classify_build_failure(
        repo, "t.cpp", "fatal error: 'Made.h' file not found") == FABRICATED


def test_a_missing_angle_include_is_a_toolchain_gap_not_a_bad_test(repo):
    """Every generated test used GoogleTest, which is absent here. That is coverage, and
    the real finding is that the project does not use GoogleTest anyway."""
    assert classify_build_failure(
        repo, "t.cpp", "fatal error: 'gtest/gtest.h' file not found") == TOOLCHAIN


def test_an_ordinary_compile_error_is_repairable(repo):
    """A typo'd fixture name and a duplicate mock definition are mechanical, deterministic
    repairs — the only failures the repair loop is allowed to see."""
    assert classify_build_failure(
        repo, "t.cpp", "error: use of undeclared identifier 'RTCMPingTest'") == REPAIRABLE


# --------------------------------------------------------------------------------------
# Orchestration, driven with a stub sandbox
# --------------------------------------------------------------------------------------


class _StubSandbox:
    """Returns canned results by call order. Never executes anything."""

    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def available(self, language):
        return True

    def run(self, workdir, argv, language, timeout_s):
        self.calls.append(argv)
        return self.results.pop(0) if self.results else RunResult(0, "", "")


def _py_repo(tmp_path):
    (tmp_path / "impl.py").write_text(
        "def add(a, b):\n    if a > 0:\n        return a + b\n    return b\n")
    (tmp_path / "test_impl.py").write_text(
        "from impl import add\n\n\ndef test_add():\n    assert add(2, 2) == 4\n")
    return tmp_path


def test_a_test_that_never_notices_a_mutant_is_vacuous(tmp_path):
    """The whole point. Baseline passes; every mutant also passes; verdict `vacuous`."""
    repo = _py_repo(tmp_path)
    sandbox = _StubSandbox([RunResult(0, "1 passed", "")] * 40)

    out = verify_test(repo, "test_impl.py", sandbox=sandbox, max_mutants=3)

    assert out.verdict == "vacuous"
    assert out.mutants_total > 0 and out.mutants_killed == 0
    assert "asserts nothing" in out.reason


def test_a_test_that_catches_a_mutant_is_useful(tmp_path):
    """Baseline passes, then mutants fail — the test noticed."""
    repo = _py_repo(tmp_path)
    sandbox = _StubSandbox([RunResult(0, "1 passed", "")]
                           + [RunResult(1, "1 failed", "")] * 40)

    out = verify_test(repo, "test_impl.py", sandbox=sandbox, max_mutants=3)

    assert out.verdict == "useful"
    assert out.mutants_killed == out.mutants_total > 0
    assert out.mutation_score == 1.0


def test_mutation_never_runs_against_a_failing_baseline(tmp_path):
    """A test that fails on correct code is `wrong`, and scoring mutants against it would
    be noise. The sandbox must not be asked to run them at all."""
    repo = _py_repo(tmp_path)
    sandbox = _StubSandbox([RunResult(1, "1 failed", "")])

    out = verify_test(repo, "test_impl.py", sandbox=sandbox, max_mutants=5)

    assert out.verdict == "wrong"
    assert out.mutants_total == 0
    assert len(sandbox.calls) == 1, "mutants were run against a broken baseline"


def test_the_users_repo_is_never_mutated(tmp_path):
    """Mutation rewrites implementation files. Doing that in place would be editing the
    user's source to run a test."""
    repo = _py_repo(tmp_path)
    before = (repo / "impl.py").read_text()
    verify_test(repo, "test_impl.py",
                sandbox=_StubSandbox([RunResult(0, "", "")] * 40), max_mutants=3)
    assert (repo / "impl.py").read_text() == before


def test_no_sandbox_means_no_verdict_not_a_pass(tmp_path):
    """Refusing to check is a fine outcome. Checking by running hostile code on the host
    is not, so there is deliberately no unsandboxed fallback."""
    class _Unavailable(_StubSandbox):
        def available(self, language):
            return False

    out = verify_test(_py_repo(tmp_path), "test_impl.py", sandbox=_Unavailable([]))
    assert out.verdict == "unverifiable"
    assert out.verdict not in ("useful", "vacuous")


def test_an_unsupported_language_is_reported_not_guessed(tmp_path):
    (tmp_path / "t.rs").write_text("fn main() {}\n")
    out = verify_test(tmp_path, "t.rs", sandbox=_StubSandbox([]))
    assert out.verdict == "unverifiable"
    assert "no verification support" in out.reason


# --------------------------------------------------------------------------------------
# Dependency closure — the link set, learned the hard way
# --------------------------------------------------------------------------------------


def test_the_closure_is_transitive_not_one_hop(tmp_path):
    """One hop does not link: the real `unicore.cpp` calls `calculateCRC32` in `crc.cpp`.
    Everything-in-the-repo does not build either. The closure is the middle."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.h").write_text("int a();\n")
    (tmp_path / "src" / "a.cpp").write_text(
        '#include "a.h"\n#include "b.h"\nint a(){return b();}\n')
    (tmp_path / "src" / "b.h").write_text("int b();\n")
    (tmp_path / "src" / "b.cpp").write_text('#include "b.h"\nint b(){return 1;}\n')
    (tmp_path / "src" / "unrelated.cpp").write_text("int z(){return 0;}\n")
    (tmp_path / "t.cpp").write_text('#include "src/a.h"\nint main(){return a();}\n')

    closure = _dependency_closure(tmp_path, "t.cpp", "C++")

    assert closure == ["src/a.cpp", "src/b.cpp"], "transitive, and nothing unrelated"


# --------------------------------------------------------------------------------------
# The sandbox contract
# --------------------------------------------------------------------------------------


def test_the_sandbox_command_carries_every_containment_flag():
    """Generated code is executed. If any of these is dropped in a later "simplification",
    a fork bomb or an exfiltrating test stops being contained.

    Asserts on the REAL command builder. An earlier version reimplemented the list and
    checked its own copy, so it passed while the actual command APPENDED argv to the
    image's ENTRYPOINT instead of replacing it — every Python verification died on
    `sh: 0: cannot open sh` and was scored `wrong`.
    """
    cmd = DockerSandbox().command(Path("/tmp/x"), ["sh", "-c", "true"], "Python")

    assert "--network" in cmd and cmd[cmd.index("--network") + 1] == "none"
    assert "--pids-limit" in cmd
    assert "--memory" in cmd
    assert "--user" in cmd and cmd[cmd.index("--user") + 1] == "65534:65534"
    # The entrypoint is REPLACED, and the image is the last thing before the args.
    assert "--entrypoint" in cmd and cmd[cmd.index("--entrypoint") + 1] == "sh"
    assert cmd[-3:] == [IMAGES["Python"], "-c", "true"], cmd[-4:]

def _click_shaped(tmp_path):
    """Two DIFFERENT `read_config` definitions, as Click's repo really has.

    This is the case that decides whether resolution goes through the import or the name.
    A name-matching implementation picks whichever it finds first and reports a mismatch
    against a function the test never called — confidently wrong on the exact file used as
    ground truth for this whole feature.
    """
    (tmp_path / "examples" / "aliases").mkdir(parents=True)
    (tmp_path / "examples" / "aliases" / "aliases.py").write_text(
        "def read_config(ctx, param, value):\n    return {}\n")
    (tmp_path / "examples" / "other").mkdir(parents=True)
    (tmp_path / "examples" / "other" / "aliases.py").write_text(
        "def read_config(path, mode):\n    return {}\n")
    return tmp_path


def test_a_hallucinated_arity_routes_to_regenerate(tmp_path):
    """The measured Python failure: the model invented a 1-arg `read_config` against a
    real 3-arg one. A repair stage told only "it raised TypeError" would patch it by
    loosening the assertion — the exact vacuity-manufacturing move we guard against."""
    from secagent.verify import check_python_test

    report = check_python_test(_click_shaped(tmp_path), (
        "from examples.aliases.aliases import read_config\n"
        "def test_it():\n    assert read_config('f.ini')\n"))

    assert report.hallucinated, "wrong arity must route to regenerate, not patch"
    assert report.issues[0].kind == "arity"
    assert "takes 3..3" in report.issues[0].detail


def test_resolution_goes_through_the_import_not_the_name(tmp_path):
    """The same call is CORRECT against the other module's 2-arg `read_config`. Only the
    import says which one the test meant."""
    from secagent.verify import check_python_test

    repo = _click_shaped(tmp_path)
    report = check_python_test(repo, (
        "from examples.other.aliases import read_config\n"
        "def test_it():\n    assert read_config('f.ini', 'r')\n"))

    assert not report.hallucinated, (
        f"resolved to the wrong module: {[i.detail for i in report.issues]}")
    assert report.checked == 1


def test_a_name_the_module_does_not_define_is_flagged(tmp_path):
    from secagent.verify import check_python_test

    report = check_python_test(_click_shaped(tmp_path), (
        "from examples.aliases.aliases import invented\n"
        "def test_it():\n    assert invented()\n"))
    assert report.issues[0].kind == "missing"


def test_third_party_and_stdlib_imports_are_not_judged(tmp_path):
    """Paired silence. `pytest`, `pathlib` and an installed `click` are not ours to check;
    claiming a mismatch there would be noise indistinguishable from a real finding."""
    from secagent.verify import check_python_test

    report = check_python_test(_click_shaped(tmp_path), (
        "from pathlib import Path\nfrom click.testing import CliRunner\n"
        "def test_it():\n    CliRunner()\n    Path('x')\n"))

    assert not report.hallucinated
    assert report.checked == 0, "nothing resolvable to this repo was called"


def test_a_correct_call_is_not_flagged(tmp_path):
    """The other paired silence: right module, right arity, no issue."""
    from secagent.verify import check_python_test

    report = check_python_test(_click_shaped(tmp_path), (
        "from examples.aliases.aliases import read_config\n"
        "def test_it():\n    assert read_config(None, None, 'v')\n"))
    assert not report.hallucinated and report.checked == 1


def test_default_arguments_widen_the_accepted_arity(tmp_path):
    from secagent.verify import check_python_test

    (tmp_path / "m.py").write_text("def f(a, b=1, c=2):\n    return a\n")
    for call in ("f(1)", "f(1, 2)", "f(1, 2, 3)"):
        r = check_python_test(tmp_path, f"from m import f\ndef test_x():\n    {call}\n")
        assert not r.hallucinated, f"{call} is legal"
    r = check_python_test(tmp_path, "from m import f\ndef test_x():\n    f(1,2,3,4)\n")
    assert r.hallucinated, "too many arguments must still be caught"


def test_a_test_with_nothing_linkable_is_not_called_uncompilable(tmp_path):
    """The harness must not blame a test for its OWN inability to build a link line.

    The dependency closure follows quoted includes. Generated C++ routinely hand-declares
    its subject instead — `extern uint32_t calculateCRC32(...)` with no `#include "crc.h"`
    — so the closure comes back empty and the build fails with undefined references that
    say nothing about the test.

    This was not hypothetical: 4 of 5 `uncompilable` verdicts in a real corpus run were
    this, and relinking one by hand against `src/crc.cpp` showed it compiles fine and
    fails its assertion — `wrong`, not `uncompilable`. Reported as-is it would have shown
    a `repairable` population of 5 and reopened a build decision on a false signal.
    """
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "crc.h").write_text("#pragma once\nunsigned crc32(unsigned char *b);\n")
    (repo / "src" / "crc.cpp").write_text(
        '#include "crc.h"\nunsigned crc32(unsigned char *b) { return b ? 1u : 0u; }\n')
    (repo / "t.cpp").write_text(
        "#include <cassert>\n"
        "extern unsigned crc32(unsigned char *b);\n"          # declared, not included
        "int main() { assert(crc32(nullptr) == 0u); return 0; }\n")

    out = verify_test(repo, "t.cpp", sandbox=_StubSandbox([]), max_mutants=3)

    assert out.verdict == "unverifiable", (
        f"an unlinkable test must not be judged; got {out.verdict}")
    assert out.verdict != "uncompilable"
    assert "no implementation could be resolved" in out.reason
    assert out.mutants_total == 0


def test_a_test_that_includes_its_header_is_still_judged(tmp_path):
    """Paired silence: refusing to judge must not become refusing everything. The same
    test, with the include it should have had, reaches the gates."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "crc.h").write_text("#pragma once\nunsigned crc32(unsigned char *b);\n")
    (repo / "src" / "crc.cpp").write_text(
        '#include "crc.h"\n'
        "unsigned crc32(unsigned char *b) {\n"
        "    if (b == nullptr) { return 0u; }\n"          # a comparison to mutate
        "    return 1u;\n}\n")
    (repo / "t.cpp").write_text(
        '#include <cassert>\n#include "src/crc.h"\n'
        "int main() { assert(crc32(nullptr) == 0u); return 0; }\n")

    sandbox = _StubSandbox([RunResult(0, "", "")] * 40)
    out = verify_test(repo, "t.cpp", sandbox=sandbox, max_mutants=3)

    assert "no implementation could be resolved" not in out.reason, (
        "a test that includes its header must not be refused for being unlinkable")
    assert out.mutants_total > 0, "the gates must have reached mutation"
    assert sandbox.calls, "the sandbox must actually have been asked to build it"


def test_an_extern_declared_subject_is_resolved_through_the_index(tmp_path):
    """When a test includes no headers, fall back to what it CALLS.

    The include-based closure is blind to `extern unsigned crc32(...)` with no
    `#include "crc.h"` — which generated C++ does routinely. The affordance index already
    records which file defines every function, so this is a reverse lookup rather than new
    analysis. On the real corpus it moved 2 of 8 unverifiable files into real verdicts,
    including one that independently reproduced a hand-relink finding: it compiles and
    fails its assertion, so `wrong`, not `uncompilable`.
    """
    from secagent.affordances.api import index_repo
    from secagent.config import Settings

    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    # A CLASS, so the index stores `Crc::Crc` and `Crc::value` while the test names them
    # unqualified — the spelling gap that makes the unqualified fallback load-bearing on
    # real C++, where every constructor call looks like this.
    (repo / "src" / "crc.h").write_text(
        "#pragma once\nclass Crc { public: Crc(); unsigned value(unsigned char *b); };\n")
    (repo / "src" / "crc.cpp").write_text(
        '#include "crc.h"\n'
        "Crc::Crc() {}\n"
        "unsigned Crc::value(unsigned char *b) {\n"
        "    if (b == nullptr) { return 0u; }\n    return 1u;\n}\n")
    (repo / "t.cpp").write_text(
        "#include <cassert>\n"
        "class Crc { public: Crc(); unsigned value(unsigned char *b); };\n"
        "int main() { Crc c = Crc(); assert(c.value(nullptr) == 0u); return 0; }\n")

    s = Settings()
    s.affordances.llm_summaries = False
    index_repo(repo, s)

    sandbox = _StubSandbox([RunResult(0, "", "")] * 40)
    out = verify_test(repo, "t.cpp", sandbox=sandbox,
                      store_dir=s.affordances.store_dir, max_mutants=3)

    assert "no implementation could be resolved" not in out.reason, (
        "the index knows which file defines crc32; it should have been linked")
    assert out.mutants_total > 0, "the gates must have reached mutation"


def test_an_unindexed_repo_still_refuses_rather_than_guessing(tmp_path):
    """Paired silence. `verify-tests` grades arbitrary repos and must not require them to
    be indexed first — but with no index and no includes there is genuinely nothing to
    link, and refusing is the honest answer."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "crc.cpp").write_text("unsigned crc32(unsigned char *b) { return 0u; }\n")
    (repo / "t.cpp").write_text(
        "#include <cassert>\nextern unsigned crc32(unsigned char *b);\n"
        "int main() { assert(crc32(nullptr) == 0u); return 0; }\n")

    out = verify_test(repo, "t.cpp", sandbox=_StubSandbox([]), max_mutants=3)

    assert out.verdict == "unverifiable"
    assert "no implementation could be resolved" in out.reason


def test_python_mutants_never_land_in_docstrings():
    """A false `vacuous` is the worst verdict this tool can produce, and it produced one.

    The line-based skip caught a line that OPENS a docstring and nothing inside one.
    Measured on Click's `_utils.py` — a 36-line enum with no comparisons, no branches and
    no logic — all four mutants landed in prose: a URL in a docstring, a return-annotation
    arrow, and two more docstring lines. None changes behaviour, so all four survived and
    a file with real assertions was called `vacuous`. That is the accusation this whole
    design exists to make credible.
    """
    src = (
        'class Sentinel(enum.Enum):\n'
        '    """Enum used for sentinels.\n'
        '\n'
        '    See `PEP 661 - Sentinel Values <https://peps.python.org/pep-0661/>`_.\n'
        '    A value but is not a flag, prompt or use the flag_value.\n'
        '    """\n'
        '\n'
        '    UNSET = object()\n'
    )
    assert mutants_for("m.py", src, "Python") == [], (
        "prose inside a docstring was mutated")


def test_a_return_annotation_arrow_is_not_a_comparison():
    """`def f() -> str:` became `-<= str:` — a syntax error dressed as a mutant, which is
    an invalid mutant at best and a surviving one at worst."""
    muts = mutants_for("m.py", "def f(a) -> str:\n    return str(a)\n", "Python")
    assert muts == [], f"the annotation arrow was mutated: {[m.after for m in muts]}"


def test_real_python_logic_is_still_mutated():
    """Paired silence, and the one that matters most: excluding prose must not become
    excluding code. A module with real branches still yields mutants."""
    src = (
        'def pick(name):\n'
        '    """Docstring with a < and an == in it."""\n'
        '    if name == "a" and len(name) > 0:\n'
        '        return 1\n'
        '    return 0\n'
    )
    muts = mutants_for("m.py", src, "Python")
    assert muts, "real comparisons must still be found"
    assert all(m.line == 3 for m in muts), f"mutants outside the code line: {muts}"


def test_a_target_with_no_logic_yields_no_mutants_not_a_vacuous_verdict():
    """`_utils.py` is an enum and two module constants. Zero mutants is the correct
    answer, and it must surface as `unverifiable` — nothing was proven — never as
    `vacuous`, which is a claim about the TEST."""
    src = "import enum\n\n\nclass S(enum.Enum):\n    A = object()\n    B = object()\n"
    assert mutants_for("m.py", src, "Python") == []
    assert verdict_for(compiled=None, ran=True, passed=True,
                       mutants_total=0, mutants_killed=0) == "unverifiable"


def test_is_none_is_a_mutation_operator():
    """`if value is None` is one of the commonest Python branches, and leaving `is` out
    of the operators made whole functions un-mutatable.

    Measured on Click's `read_config(ctx, param, value)`, whose only branch is
    `if value is None`: it produced ZERO mutants, so a genuinely good test of it and a
    vacuous one both scored `vacuous`, indistinguishable — the exact false the tool
    exists to avoid, reached through the mutation gate rather than a docstring.
    """
    muts = mutants_for("m.py", "def f(v):\n    if v is None:\n        return 1\n    return v\n",
                       "Python")
    assert any(m.operator == "identity-flip" for m in muts), (
        f"`is None` was not mutated: {[(m.line, m.operator) for m in muts]}")
    flip = next(m for m in muts if m.operator == "identity-flip")
    assert "is not None" in flip.after


def test_is_not_none_is_not_double_mutated_into_is_not_not():
    """`is not None` is already covered by the `not`-drop; the bare-`is` operator must not
    also fire on it and produce `is not not None`."""
    muts = mutants_for("m.py", "def f(v):\n    if v is not None:\n        return 1\n    return 0\n",
                       "Python")
    assert all("is not not" not in m.after for m in muts), (
        f"double-mutated: {[m.after for m in muts]}")
