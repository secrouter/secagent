"""Load and present the configurable scan rule set.

A rule set is plain YAML (see ``config/rules/*.yaml``), loaded fresh on each run so
edits take effect immediately — the same pattern as the review persona.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml

from ...config import resolve_config_path

# Below this, one call is comfortably within budget and splitting only adds round trips.
_MIN_RULES_TO_SPLIT = 8

# Severity order for deciding which findings survive a per-file cap.
SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


@dataclass
class Rule:
    id: str
    title: str
    category: str
    severity: str
    guidance: str
    references: list[str] = field(default_factory=list)


@dataclass
class RuleSet:
    name: str
    description: str
    rules: list[Rule]
    min_severity: str = "low"
    max_findings_per_file: int = 20
    focus: list[str] = field(default_factory=list)
    # Languages this profile applies to. The scan only sends files of these languages to
    # the model, so a C-oriented profile never applies malloc/free rules to Rust (and
    # vice versa). Defaults to C/C++ for back-compat with existing profiles.
    languages: list[str] = field(default_factory=lambda: ["C", "C++"])

    def by_id(self) -> dict[str, Rule]:
        return {r.id: r for r in self.rules}


def load_rules(profile_path: str | Path) -> RuleSet:
    """Load a rule set from YAML; tolerant of missing optional keys."""
    p = resolve_config_path(profile_path)
    if not p.exists():
        raise FileNotFoundError(f"rules profile not found: {p}")
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    settings = data.get("settings", {}) or {}
    rules: list[Rule] = []
    for item in data.get("rules", []) or []:
        if not isinstance(item, dict) or "id" not in item:
            continue
        rules.append(
            Rule(
                id=str(item["id"]),
                title=str(item.get("title", item["id"])),
                category=str(item.get("category", "general")),
                severity=str(item.get("severity", "medium")).lower(),
                guidance=str(item.get("guidance", "")).strip(),
                references=[str(r) for r in (item.get("references") or [])],
            )
        )
    return RuleSet(
        name=str(data.get("name", p.stem)),
        description=str(data.get("description", "")).strip(),
        rules=rules,
        min_severity=str(settings.get("min_severity", "low")).lower(),
        max_findings_per_file=int(settings.get("max_findings_per_file", 20)),
        focus=[str(x) for x in (settings.get("focus") or [])],
        languages=[str(x) for x in (settings.get("languages") or ["C", "C++"])],
    )


def rules_prompt(rs: RuleSet) -> str:
    """Compact rule listing for the scanner's system prompt."""
    lines = [f"Rule set: {rs.name}. Apply ONLY these rules:"]
    for r in rs.rules:
        lines.append(f"- {r.id} [{r.severity}/{r.category}] {r.title}: {r.guidance}")
    return "\n".join(lines)


def rule_groups(rs: RuleSet, granularity: str | bool = "category") -> list[tuple[str, RuleSet]]:
    """Split a profile into sub-profiles: all rules at once, per category, or per rule.

    **Correcting the record.** This docstring used to claim splitting was "not a speed
    optimisation; it is the difference between an answer and no answer", citing a single
    call that exhausted a 16000-token budget and returned empty. That was measured with
    `llm.temperature` at its 0.2 default, and it was wrong. At 0.2 the model fell into
    degenerate repetition — one run repeated a single finding line 334 times before
    hitting the cap — and returned nothing. The failure was the sampling temperature, not
    the prompt size. At temperature 1.0 the same 32 rules in one call answer fine
    (6275 tokens, 67s), and per-category groups go from 0 of 5 converging to 5 of 5.

    The honest justification is **recall against cost**, measured on the same file:

        all       32 rules, 1 call     67s     3 findings
        category  7 calls              8.5min  8 findings
        rule      32 calls             23.2min 16 findings

    Finer prompts surface more. Whether the extra findings are TRUE is a separate
    question nobody has fully answered. Measured C precision on cFS at a converging
    temperature is 25-50% depending on whether a missing NULL check no caller can reach
    counts as real (quality/PRECISION_C_TEMP10.md) — and six of the nine true positives
    there were TWO defects reported by every category that could see them, so a finding
    count overstates the gain. More findings may simply be more noise, or the same noise
    counted more times. `category` is the default because it is what shipped, not
    because it is known to be the best point on that curve.

    Splitting does still make failure attributable: when some groups exhaust their
    budget the report names which ones went unexamined instead of dropping the file.
    That part was always true and is independent of temperature.

    ``granularity`` accepts the legacy ``bool`` (True -> category, False -> all) so
    existing callers and the measurement probes keep working.
    """
    if isinstance(granularity, bool):
        granularity = "category" if granularity else "all"
    if granularity == "all" or len(rs.rules) <= _MIN_RULES_TO_SPLIT:
        return [("", rs)]
    if granularity == "rule":
        # One call per rule. Named by rule id, so a failure names the exact rule that
        # went unapplied rather than a whole category.
        return [(r.id, replace(rs, rules=[r])) for r in sorted(rs.rules, key=lambda r: r.id)]
    groups: dict[str, list[Rule]] = {}
    for r in rs.rules:
        groups.setdefault(r.category or r.id.split("-")[0], []).append(r)
    if len(groups) < 2:
        return [("", rs)]
    return [(cat, replace(rs, rules=rules)) for cat, rules in sorted(groups.items())]


def meets_threshold(severity: str, min_severity: str) -> bool:
    return SEVERITY_ORDER.get(severity, 9) <= SEVERITY_ORDER.get(min_severity, 3)
