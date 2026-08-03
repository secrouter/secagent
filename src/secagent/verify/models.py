"""What a verified test is, and what we are allowed to say about it.

The vocabulary exists because `ok: true` used to mean "the model emitted text". Measured on
one real generated C++ file, of 10 test cases 3 were actively wrong, 3 were vacuous and 4
were useful — and `ok: true` was recorded for all of them. Three of those six failure modes
are invisible to a compile gate, and three of them are invisible to a run gate too.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# The verdicts, in descending order of "this test is doing its job".
#
#   useful        compiles, passes against correct code, and kills at least one mutant
#   vacuous       compiles, passes, and kills NOTHING — the dangerous one, because it is
#                 green in CI and asserts nothing. Three of ten real generated tests.
#   wrong         compiles but FAILS against correct, unmodified code — a false CI failure
#   uncompilable  did not build
#   unrunnable    hung, crashed the harness, or tripped a sandbox limit
#   unverifiable  no toolchain for this language/framework here. COVERAGE, NOT A PASS.
#   environment   did not build because something the REPO ITSELF needs is absent — e.g.
#                 PX4's `definitions.h`, supplied by the parent project and not vendored
#                 here. Not a bad test, and reporting it as one would blame the generator
#                 for a missing dependency. Terminal: no repair can fix the environment.
VERDICTS = ("useful", "vacuous", "wrong", "uncompilable", "unrunnable", "unverifiable",
            "environment")

# The verdicts that mean "we successfully reached a judgement". `unverifiable` is
# deliberately absent: not being able to check something is not a result about it.
_JUDGED = ("useful", "vacuous", "wrong")


@dataclass
class TestOutcome:
    """One test file, and everything the gates learned about it."""

    # Not a pytest test class, despite the name. Without this pytest tries to collect it
    # and warns on every run.
    __test__ = False

    path: str
    language: str
    verdict: str
    # Why this verdict. Always populated for anything that is not `useful`, because a
    # bare verdict with no cause is the failure this project keeps fixing.
    reason: str = ""

    compiled: bool | None = None      # None = not applicable (Python has no build step)
    ran: bool = False
    passed_on_correct_code: bool = False

    # Mutation. `total` counts only VALID mutants — a mutant that fails to build proves
    # nothing about the test and must not sit in the denominator making a good test look
    # weak. Invalid ones are counted separately so the ratio can be audited.
    mutants_total: int = 0
    mutants_killed: int = 0
    mutants_invalid: int = 0
    # Which mutations the test failed to notice. The actionable half of the score: a
    # reader can look at these and decide whether they matter.
    survived: list[str] = field(default_factory=list)

    # Recorded now, used by the (deferred) repair loop: a repair must never reduce
    # either of these. See `quality/TEST_VERIFICATION_DESIGN.md`.
    assertions_n: int = 0
    # Why the build failed, when it did. Decides whether a repair loop may see this file
    # at all — see `verify/repair.py`. One of: "", "environment", "fabricated-include",
    # "repairable".
    failure_cause: str = ""

    @property
    def mutation_score(self) -> float | None:
        """Killed / valid mutants, or None when nothing valid could be built.

        Reported as a score rather than a boolean on purpose. "Survived mutation" is a
        cliff that gets gamed the moment anyone optimises against it; "killed 7 of 10" is
        a quality signal a reader can act on, and it gives a repair loop a gradient to
        climb rather than a line to step over.
        """
        if self.mutants_total <= 0:
            return None
        return self.mutants_killed / self.mutants_total

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        score = self.mutation_score
        d["mutation_score"] = None if score is None else round(score, 3)
        return d


def verdict_for(
    *,
    compiled: bool | None,
    ran: bool,
    passed: bool,
    mutants_total: int,
    mutants_killed: int,
) -> str:
    """The single place a verdict is decided, so the ladder cannot drift between callers.

    Order matters: each rung is only meaningful if the one below it held. A test that did
    not compile has no run result; one that fails on correct code is `wrong` whatever
    mutation would have said, because mutation scores against a broken baseline are noise.
    """
    if compiled is False:
        return "uncompilable"
    if not ran:
        return "unrunnable"
    if not passed:
        return "wrong"
    if mutants_total <= 0:
        # Passed clean, but nothing could be mutated — usually no target source resolved.
        # Not `useful` (unproven) and not `vacuous` (unmeasured). Say so.
        return "unverifiable"
    return "useful" if mutants_killed > 0 else "vacuous"
