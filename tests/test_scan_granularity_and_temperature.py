"""Scan's sampling temperature, and how finely it sends its rules.

Both settings exist because of one mis-diagnosis. `scan` returned empty on C++ and that
was blamed, in order, on file size, on concurrency settings, and on the model being too
small for parser code. It was none of those: `llm.temperature` defaulting to 0.2 drove the
model into degenerate repetition — a measured run repeated one finding line 334 times,
hit the 16384-token cap, and returned nothing. At temperature 1.0 the same file and the
same 32 rules answer in 6275 tokens and 67 seconds.

So temperature is now scoped to rule-checking calls (summarisation may legitimately want
determinism; "is this code safe?" does not), and rule granularity became a three-way
choice rather than a boolean, because what splitting actually buys is recall against wall
time — not, as previously documented, the difference between an answer and no answer.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from secagent.agents.scan.agent import scan_repo
from secagent.agents.scan.rules import load_rules, rule_groups
from secagent.config import Settings

from .conftest import make_chat_response, mock_client

REPO_ROOT = Path(__file__).resolve().parents[1]
RULES = REPO_ROOT / "config" / "rules" / "embedded-cpp.yaml"


def _settings(tmp_path) -> Settings:
    s = Settings()
    s.affordances.llm_summaries = False
    s.affordances.store_dir = str(tmp_path / "store")
    s.scan.rules_profile = str(RULES)
    return s


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    (repo / "a.c").write_text("int main(void) { return 0; }\n")
    return repo


def _recording_llm(bodies: list[dict], **cfg):
    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=make_chat_response(content="[]"))
    return mock_client(handler, **cfg)


# --- granularity ---------------------------------------------------------------

def test_the_three_granularities_split_the_profile_differently():
    """`all` sends one call, `category` one per category, `rule` one per rule. The
    profile is the real shipped `embedded-cpp.yaml`, not a synthetic rule list, because
    the split keys off each rule's own category field."""
    rs = load_rules(RULES)
    n_rules = len(rs.rules)
    assert n_rules > 2, "the shipped profile must be big enough for splitting to differ"

    assert len(rule_groups(rs, "all")) == 1
    per_rule = rule_groups(rs, "rule")
    assert len(per_rule) == n_rules
    assert all(len(sub.rules) == 1 for _, sub in per_rule)
    assert [cat for cat, _ in per_rule] == sorted(r.id for r in rs.rules), \
        "a per-rule group must be named by its rule id, so a failure names the rule"

    per_cat = rule_groups(rs, "category")
    assert 1 < len(per_cat) < n_rules
    assert sum(len(sub.rules) for _, sub in per_cat) == n_rules, "no rule may be lost"


def test_the_deprecated_boolean_still_selects_the_same_split():
    """`split_rules_by_category` was the old spelling. It keeps working — silently
    changing an existing config's behaviour would be worse than carrying an alias."""
    rs = load_rules(RULES)
    assert rule_groups(rs, True) == rule_groups(rs, "category")
    assert rule_groups(rs, False) == rule_groups(rs, "all")


def test_the_deprecated_boolean_is_honoured_only_until_the_new_setting_is_used():
    """Resolution happens at use time, not construction: callers set these by assignment
    on an already-built Settings, and a construction-time validator would silently not
    apply to that. An explicit `rule_granularity` wins; the boolean applies when it alone
    was set; neither set means the shipped default."""
    assert Settings().scan.effective_granularity() == "category"

    old_style = Settings()
    old_style.scan.split_rules_by_category = False
    assert old_style.scan.effective_granularity() == "all"

    new_style = Settings()
    new_style.scan.rule_granularity = "rule"
    assert new_style.scan.effective_granularity() == "rule"

    both = Settings()
    both.scan.split_rules_by_category = False
    both.scan.rule_granularity = "rule"
    assert both.scan.effective_granularity() == "rule", "the explicit new setting wins"


def test_granularity_changes_how_many_calls_a_scan_makes(tmp_path):
    """End to end through `scan_repo`: the setting has to reach the model, not just the
    splitter. One file, so the call count is the group count."""
    rs = load_rules(RULES)
    counts = {}
    for mode in ("all", "category", "rule"):
        bodies: list[dict] = []
        s = _settings(tmp_path)
        s.scan.rule_granularity = mode
        scan_repo(_repo(tmp_path / mode), s, out_dir=tmp_path / f"o-{mode}",
                  llm=_recording_llm(bodies))
        counts[mode] = len(bodies)

    assert counts["all"] == 1
    assert counts["category"] == len(rule_groups(rs, "category"))
    assert counts["rule"] == len(rs.rules)
    assert counts["all"] < counts["category"] < counts["rule"], \
        "finer granularity costs strictly more calls — that is the trade being made"


def test_the_shipped_default_is_still_per_category(tmp_path):
    """The silence half of the granularity change: an unconfigured scan behaves exactly
    as it did before this setting existed, so nobody's findings move underneath them."""
    bodies: list[dict] = []
    scan_repo(_repo(tmp_path), _settings(tmp_path), out_dir=tmp_path / "o",
              llm=_recording_llm(bodies))
    assert len(bodies) == len(rule_groups(load_rules(RULES), "category"))


# --- temperature ---------------------------------------------------------------

def test_scan_sends_its_own_temperature_not_the_global_one(tmp_path):
    """The whole point of the setting: rule-checking gets a temperature chosen for
    reasoning about safety, while `llm.temperature` stays low for summarisation. Read off
    the real serialised request body, so this fails if the value is dropped anywhere
    between the setting and the wire."""
    bodies: list[dict] = []
    s = _settings(tmp_path)
    s.llm.temperature = 0.2          # global stays deterministic
    s.scan.temperature = 0.9
    scan_repo(_repo(tmp_path), s, out_dir=tmp_path / "o", llm=_recording_llm(bodies))

    assert bodies, "the scan must actually have called the model"
    assert {b["temperature"] for b in bodies} == {0.9}


def test_scan_temperature_zero_falls_back_to_the_client_default(tmp_path):
    """0 means "follow the client's own temperature" rather than "sample at 0" — a scan
    config that never mentions temperature must not silently pin it to 0.

    The fallback is read off the LLM client actually in use, not `settings.llm`: an
    injected client carries its own `LLMConfig`, and asserting against the settings
    object would have passed for the wrong reason here."""
    bodies: list[dict] = []
    s = _settings(tmp_path)
    s.scan.temperature = 0.0
    scan_repo(_repo(tmp_path), s, out_dir=tmp_path / "o",
              llm=_recording_llm(bodies, temperature=0.3))

    assert {b["temperature"] for b in bodies} == {0.3}


def test_the_shipped_scan_default_is_not_the_repetition_trap(tmp_path):
    """The regression that matters. 0.2 is the value that produced 334 repetitions of one
    line and an empty response; the scan default must not be it, and must not silently
    inherit it from the global either."""
    bodies: list[dict] = []
    s = _settings(tmp_path)
    scan_repo(_repo(tmp_path), s, out_dir=tmp_path / "o", llm=_recording_llm(bodies))

    sent = {b["temperature"] for b in bodies}
    assert sent == {s.scan.temperature}
    assert s.scan.temperature >= 0.7, "measured: 0.2 degenerates, 0.7 and 1.0 converge"
    assert 0.2 not in sent, "0.2 is the measured repetition trap; scan must not send it"
