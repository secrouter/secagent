"""Coverage and provenance primitives shared by every command's output.

This repo has shipped the same defect five separate times under five different
names: a scanner that truncated files silently, a testgen that dropped
unreadable files without a trace, a docs build that rendered 9 of 17 modules
and called it done, and an `analyze` pass that reported `tus_skipped: 0` for
files that were never in the compile database to begin with. Each command
invented its own coverage shape, by hand, from integers someone counted
somewhere — and each one was wrong in a different place.

The risk this module exists to prevent is not "no coverage block" — it is a
*fake* coverage block: plausible-looking numbers that were never actually
tallied against real per-unit outcomes, sitting in a report next to numbers
that were. A fabricated 0 is strictly worse than an honest, ad-hoc shape,
because it reads as confirmation instead of as the absence of an answer. So
`Coverage` validates its own numbers on construction (`__post_init__`, not a
lint pass someone can skip) and `Coverage.from_why` gives producers that know
every unit's fate a way to *derive* the counts instead of typing them.

Each command's output has one or more "unit domains" — populations counted in
different units answering different trust questions. `secagent analyze scan`
has translation-unit coverage (did we compile it) and finding-triage coverage
(did we adjudicate it) over the same run; `docs` has four. There is
deliberately no single per-command coverage block: `coverage_to_dict` renders
a mapping of domain name -> `Coverage`, and each domain's `why` is keyed by
whatever that domain's unit id actually is (a file path for `files`, a
`file:line` finding id for `triage`, a function name for `functions`) — never
assumed to be a file path.

`Observation`, `Envelope`, `Location`, and a severity enum are explicitly out
of scope for this module; they are deferred to a later PR that retrofits this
into the agents.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

# The four possible fates of a unit that was actually attempted, plus
# "skipped" for a unit that was never attempted at all. `attempted` is
# deliberately NOT one of these — it is derived as
# succeeded + partial + failed, so it can never independently drift from the
# states that make it up (the drift that made scan.json's own
# files_analyzed/files_failed/files_partial trio inconsistent with each other
# before this module existed).
STATES = ("succeeded", "partial", "failed", "skipped")

# `Coverage.from_why(why)` needs to distinguish "the caller did not pass
# `eligible`, so derive it from `why`" from "the caller explicitly passed
# `eligible=None`, meaning the population truly cannot be known". Both cases
# must be expressible, and `None` is the correct value for the second one, so
# it cannot also be the "omitted" default. A private sentinel type is the
# only way to tell the two apart at the call site, and gives mypy an actual
# type to check the parameter against instead of a bare `object`.
class _Unset:
    __slots__ = ()


_UNSET = _Unset()


@dataclass(frozen=True)
class UnitState:
    """The recorded fate of one unit within a `Coverage` block's ``why`` map.

    ``reason`` must name the *cause*, not restate the state. "skipped" tells
    a reader nothing they didn't already know from the key existing in a
    "skipped" bucket; "scan.max_files=3" tells them what to change. A vague
    reason here is the same failure mode as a fake coverage number: it looks
    like an answer but carries none of the information an answer should.
    """

    state: str
    reason: str


@dataclass(frozen=True)
class Coverage:
    """Per-unit-domain coverage: what a command was asked to cover, and what
    actually happened to each unit.

    ``eligible`` is the size of the population this command *could* have
    covered. It is ``int | None`` because for some producers that number is
    not merely large or unknown-but-knowable — it is genuinely unknowable
    from where the producer sits. `secagent analyze ingest` receives findings
    from an external SARIF report; it never sees the compiler's translation
    units and has no honest way to say how many there "should" have been.
    Inventing a number there — even a conservative one — is precisely the
    fake-coverage failure mode this module exists to prevent, so `eligible`
    stays `None` and `complete` is unconditionally `False` rather than
    inferred from the other counts.
    """

    eligible: int | None
    attempted: int
    succeeded: int
    partial: int
    failed: int
    skipped: int
    why: dict[str, UnitState] = field(default_factory=dict)

    def __post_init__(self) -> None:
        counts = {
            "attempted": self.attempted,
            "succeeded": self.succeeded,
            "partial": self.partial,
            "failed": self.failed,
            "skipped": self.skipped,
        }
        for name, value in counts.items():
            if value < 0:
                raise ValueError(f"Coverage.{name} must be >= 0, got {value}")
        if self.eligible is not None and self.eligible < 0:
            raise ValueError(f"Coverage.eligible must be None or >= 0, got {self.eligible}")

        # attempted is derived, not independently reported, so a producer that
        # types "attempted: 8" while its own succeeded/partial/failed sum to 7
        # is caught here rather than shipped as a report nobody re-adds.
        derived_attempted = self.succeeded + self.partial + self.failed
        if self.attempted != derived_attempted:
            raise ValueError(
                f"Coverage.attempted={self.attempted} does not equal "
                f"succeeded({self.succeeded}) + partial({self.partial}) + "
                f"failed({self.failed}) = {derived_attempted}"
            )

        # When eligible IS known, it must reconcile with attempted + skipped —
        # this is the exact shape of bug that let a run report
        # files_skipped_by_cap derived from selection while files_analyzed was
        # derived from what ran, with nothing forcing the two to agree.
        if self.eligible is not None:
            derived_eligible = self.attempted + self.skipped
            if self.eligible != derived_eligible:
                raise ValueError(
                    f"Coverage.eligible={self.eligible} does not equal "
                    f"attempted({self.attempted}) + skipped({self.skipped}) = "
                    f"{derived_eligible}"
                )

        for unit_id, unit_state in self.why.items():
            if unit_state.state not in STATES:
                raise ValueError(
                    f"Coverage.why[{unit_id!r}].state={unit_state.state!r} is not one of "
                    f"{STATES}"
                )
            if not unit_state.reason.strip():
                raise ValueError(
                    f"Coverage.why[{unit_id!r}] has an empty reason — a unit's fate must "
                    "name a cause, not just a state"
                )

        # A producer that names three failed units in `why` but reports
        # `failed: 1` is undercounting its own failures — the report's headline
        # number and its own detail disagree, and the detail is presumably the
        # one that is actually true. Catch it here rather than downstream.
        #
        # Rule 5 is `>`, not `!=` / `==`. This is deliberate, not a hedge: `why` is
        # keyed by unit id, and unit ids are NOT unique per unit of work. A compile
        # database can legitimately list the same file twice under different flags
        # (the `translation_units` domain keys `why` by the relativized path), and
        # two distinct IKOS findings can share a `file:line` (the `triage` domain's
        # key). Either way, a dict-keyed `why` collapses two real, distinct units
        # into one entry — so `len(why[state])` can honestly be LESS than the
        # reported count on a completely honest run. An equality rule here would
        # turn that honest collision into a spurious "INTERNAL BUG — coverage
        # omitted" and silently drop the whole coverage block from a good run. `>`
        # still catches the failure mode rule 5 exists for (why claiming MORE units
        # than the headline admits to) without punishing collisions it cannot avoid.
        why_counts = dict.fromkeys(STATES, 0)
        for unit_state in self.why.values():
            why_counts[unit_state.state] += 1
        for state in STATES:
            reported = counts[state]
            if why_counts[state] > reported:
                raise ValueError(
                    f"Coverage.why names {why_counts[state]} unit(s) with state "
                    f"{state!r}, but Coverage.{state}={reported} — why cannot name more "
                    "units than the count it details"
                )

    @property
    def complete(self) -> bool:
        """Whether every eligible unit was attempted and none was partial/failed.

        Never a stored field: a stored `complete` is one more value a producer
        could set inconsistently with the counts it sits beside. Computed, it
        cannot drift from them. `eligible is None` always yields `False` —
        completeness over an unknowable population is not a claim this module
        will make on a producer's behalf.
        """
        return (
            self.eligible is not None
            and self.attempted == self.eligible
            and self.partial == 0
            and self.failed == 0
        )

    def to_dict(self, *, include_why: bool = True) -> dict[str, Any]:
        """Render the JSON shape written under a domain key in ``coverage``.

        ``include_why=False`` drops the ``why`` map entirely. The full block
        (with ``why``) belongs in the JSON artifact on disk; the dict a
        command *returns* is printed to CLI stdout, which the pi extension
        forwards verbatim into a model's context, so it has to stay bounded
        regardless of how many units the run touched.
        """
        out: dict[str, Any] = {
            "eligible": self.eligible,
            "attempted": self.attempted,
            "succeeded": self.succeeded,
            "partial": self.partial,
            "failed": self.failed,
            "skipped": self.skipped,
            "complete": self.complete,
        }
        if include_why:
            out["why"] = {
                unit_id: {"state": s.state, "reason": s.reason}
                for unit_id, s in self.why.items()
            }
        return out

    @classmethod
    def from_why(
        cls, why: Mapping[str, UnitState], *, eligible: int | None | _Unset = _UNSET
    ) -> Coverage:
        """Derive a `Coverage` by tallying ``why`` instead of hand-counting it.

        Available for a producer that enumerates every unit's outcome into
        ``why`` up front and wants the counts derived from that map by
        arithmetic, rather than typed a second time by hand. No current
        producer is shaped that way: `scan`, `testgen`, and `analyze` all
        hand-count `eligible`/`attempted`/`succeeded`/`partial`/`failed`/
        `skipped` from the same dicts they build `why` from (see
        `agents/scan/agent.py`, `agents/testgen/agent.py`,
        `agents/analysis/agent.py`), and call `Coverage(...)` directly with
        those counts. This docstring used to call `from_why` "the
        anti-fabrication constructor" as if it were what makes a fake coverage
        block impossible — it has no production caller, so that guarantee
        does not come from here. It comes from `tests/test_coverage_blocks.py`'s
        `_reconstruct`: each producer's own reconstruct test rebuilds a
        `Coverage` from the JSON the command actually wrote and re-validates it
        against `Coverage.__post_init__`, which is what actually catches a
        producer's hand-count drifting from its own `why`.

        ``eligible`` defaults to "the number of units named in ``why``" —
        appropriate when ``why`` truly enumerates the whole eligible
        population (scan's file list). Passing ``eligible=None`` explicitly
        still means "unknowable" and is preserved as `None`, so the two cases
        — "derive it from why" and "it cannot be known" — stay distinguishable
        at the call site. `None` cannot be the default for that reason: it is
        also a legal explicit value with a different meaning.
        """
        tallies = dict.fromkeys(STATES, 0)
        for unit_state in why.values():
            tallies[unit_state.state] += 1
        succeeded, partial, failed, skipped = (
            tallies["succeeded"],
            tallies["partial"],
            tallies["failed"],
            tallies["skipped"],
        )
        attempted = succeeded + partial + failed
        resolved_eligible: int | None = len(why) if isinstance(eligible, _Unset) else eligible
        return cls(
            eligible=resolved_eligible,
            attempted=attempted,
            succeeded=succeeded,
            partial=partial,
            failed=failed,
            skipped=skipped,
            why=dict(why),
        )


def coverage_to_dict(
    domains: Mapping[str, Coverage], *, include_why: bool = True
) -> dict[str, Any]:
    """Render a whole multi-domain ``coverage`` block.

    One small helper so that a command with several unit domains (analyze's
    ``files`` + ``triage``, docs's four populations) does not re-implement
    this loop at each call site with its own small variation.
    """
    return {name: cov.to_dict(include_why=include_why) for name, cov in domains.items()}


@dataclass(frozen=True)
class Provenance:
    """Where a report's content actually came from.

    Two evaluation agents each burned a run unable to tell whether the output
    in front of them reflected a working model, a broken endpoint, or the
    heuristic fallback quietly taking over. Every field here answers one part
    of that question directly rather than requiring it be reconstructed from
    logs.
    """

    secagent_version: str
    model: list[str]
    endpoint: str
    generated_at: str
    heuristic_only: bool

    def __post_init__(self) -> None:
        # `model` is a list, deliberately: a docs run renders pages from
        # per-file summaries that were cached at INDEX time, possibly under a
        # different model than the one running THIS render; a triaged analyze
        # run mixes a no-model producer (IKOS, static) with a triage model. A
        # single string cannot name both. Rejecting a bare `str` is not
        # pedantry — `isinstance(x, list)` is True for neither a bare string
        # nor is a string itself safely iterable here: `for m in "gemma"`
        # yields `"g", "e", "m", "m", "a"`, which is a real, silent way this
        # has broken before once something downstream assumes a list.
        if isinstance(self.model, str):
            raise ValueError(
                f"Provenance.model must be a list[str], got a bare str {self.model!r} — "
                "iterating it yields individual characters, not model names"
            )
        if not isinstance(self.model, list) or not all(
            isinstance(m, str) and m.strip() for m in self.model
        ):
            raise ValueError(
                f"Provenance.model must be a list of non-empty strings, got {self.model!r}"
            )

        if self.heuristic_only and self.model:
            raise ValueError(
                f"Provenance.heuristic_only=True is contradictory with a non-empty "
                f"model list {self.model!r} — a heuristic-only result was not produced "
                "by any of the named models"
            )

        if not self.generated_at.endswith("Z"):
            raise ValueError(
                f"Provenance.generated_at={self.generated_at!r} must be an ISO-8601 UTC "
                "timestamp ending in 'Z'"
            )
        try:
            # datetime.fromisoformat does not accept a bare 'Z' suffix before
            # Python 3.11's relaxed parser; normalise the same way `now()`
            # produces it so validation actually exercises the format we emit.
            datetime.fromisoformat(self.generated_at[:-1] + "+00:00")
        except ValueError as exc:
            raise ValueError(
                f"Provenance.generated_at={self.generated_at!r} is not a valid ISO-8601 "
                f"timestamp: {exc}"
            ) from exc

    @classmethod
    def now(
        cls, *, model: list[str], endpoint: str, heuristic_only: bool = False
    ) -> Provenance:
        """Build a `Provenance` stamped with the current package version and time.

        Pulls `secagent_version` from `secagent.__version__` — the package's one
        existing source of truth for its own version (also used by
        `secagent --version` in `cli.py`) — rather than inventing a second place
        that could disagree with it.
        """
        from . import __version__

        generated_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        return cls(
            secagent_version=__version__,
            model=model,
            endpoint=endpoint,
            generated_at=generated_at,
            heuristic_only=heuristic_only,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "secagent_version": self.secagent_version,
            "model": list(self.model),
            "endpoint": self.endpoint,
            "generated_at": self.generated_at,
            "heuristic_only": self.heuristic_only,
        }
