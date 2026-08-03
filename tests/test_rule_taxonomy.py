"""The rule profiles must say what kind of thing each rule reports.

Measured precision on cFS found the two biggest sources of noise were both rule-TEXT
problems rather than sampling problems: `CTL-004` asked about ignored return values in
general, which in cFS is an idiom and not a defect, and nine `PTR-001` findings were
"exported function lacks a NULL check no caller can reach" reported at HIGH severity under
a memory-safety rule. See `quality/PRECISION_C_TEMP10.md`.

The fix was to draw a line between a **reachable defect** and **interface hardening**, and
to route the second to `CTL-005` at low severity. These tests pin the structural half of
that: that the profiles parse, that the taxonomy's severities are what it says, and that
a rule which points the reader at another rule points at one that exists.

They are deliberately NOT evidence that the change works. Whether the model actually
files findings the way the text asks is a measurement, not a unit test, and it lives in
`quality/PRECISION_C_TEMP10.md`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from secagent.agents.scan.rules import load_rules

PROFILES = sorted((Path(__file__).resolve().parents[1] / "config" / "rules").glob("*.yaml"))
SEVERITIES = {"critical", "high", "medium", "low", "info"}


def test_every_shipped_profile_is_present():
    """A guard on the parametrisation itself: if the glob silently found nothing, every
    test below would pass vacuously."""
    assert len(PROFILES) >= 3, f"expected the shipped profiles, found {PROFILES}"


@pytest.mark.parametrize("profile", PROFILES, ids=lambda p: p.name)
def test_every_rule_is_completely_specified(profile):
    """A rule missing its guidance is a rule the model is asked to apply from its title
    alone — which is how `CTL-004` came to mean 'any ignored return value'."""
    rs = load_rules(profile)
    assert rs.rules, f"{profile.name} has no rules"
    for r in rs.rules:
        assert r.id and r.title and r.category, f"{profile.name}: incomplete rule {r!r}"
        assert r.guidance.strip(), f"{profile.name}: {r.id} has no guidance"
        assert r.severity in SEVERITIES, f"{profile.name}: {r.id} severity {r.severity!r}"


@pytest.mark.parametrize("profile", PROFILES, ids=lambda p: p.name)
def test_rule_ids_are_unique(profile):
    rs = load_rules(profile)
    ids = [r.id for r in rs.rules]
    assert len(ids) == len(set(ids)), f"{profile.name}: duplicate ids in {ids}"


@pytest.mark.parametrize("profile", PROFILES, ids=lambda p: p.name)
def test_a_rule_that_redirects_to_another_names_one_that_exists(profile):
    """The taxonomy only works if the signposts point somewhere.

    `PTR-001` tells the model to report `CTL-005` instead when a bad value is not
    reachable, and `CTL-005` points back at `PTR-001`/`BUF-002`. Rename or drop one of
    those and the guidance silently instructs the model to use a rule that does not
    exist — which it cannot report, so the finding would just vanish. This is the failure
    a future edit is most likely to cause and least likely to notice.
    """
    rs = load_rules(profile)
    known = {r.id for r in rs.rules}
    pattern = re.compile(r"\b([A-Z]{3}-\d{3})\b")
    for r in rs.rules:
        referenced = set(pattern.findall(r.guidance)) - {r.id}
        missing = referenced - known
        assert not missing, (
            f"{profile.name}: {r.id}'s guidance redirects to {sorted(missing)}, "
            f"which does not exist in this profile"
        )


def test_hardening_is_low_severity_and_the_defect_rules_are_not():
    """The severity difference IS the taxonomy. A finding that says 'null pointer
    dereference' at high severity about code nothing can reach is wrong; the same
    observation filed as 'this public interface is unvalidated' at low severity is
    useful. If these ever collapse to the same severity, the split has stopped meaning
    anything even if the wording still describes it."""
    rs = load_rules(Path(__file__).resolve().parents[1] / "config/rules/embedded-cpp.yaml")
    by_id = {r.id: r for r in rs.rules}

    assert by_id["CTL-005"].severity == "low", "hardening must not be reported as a defect"
    for defect in ("PTR-001", "BUF-002"):
        assert by_id[defect].severity == "high", f"{defect} reports reachable defects"
        assert by_id[defect].severity != by_id["CTL-005"].severity


def test_the_return_value_rule_excludes_the_idioms_it_used_to_flag():
    """`CTL-004`'s eleven false positives were eight `CFE_EVS_SendEvent` calls and one
    `(void)`-cast call carrying a suppression comment. The guidance has to name those
    exclusions, because a model cannot infer them from 'check return values'."""
    rs = load_rules(Path(__file__).resolve().parents[1] / "config/rules/embedded-cpp.yaml")
    guidance = {r.id: r.guidance for r in rs.rules}["CTL-004"].lower()

    assert "(void)" in guidance, "must exclude explicitly discarded returns"
    assert "telemetry" in guidance or "event" in guidance, "must exclude event/telemetry emits"
    # ...and must still ask for the three findings that made the rule worth keeping.
    assert "semaphore" in guidance or "lock" in guidance, "must still catch OS_MutSemTake"
    assert "release" in guidance or "close" in guidance, "must still catch OS_DirectoryClose"
