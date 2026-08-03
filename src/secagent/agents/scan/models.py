"""Finding model for the LLM rule-based scan (UC4)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .rules import SEVERITY_ORDER


@dataclass
class ScanFinding:
    rule_id: str
    category: str
    severity: str
    file: str
    line: int = 0
    message: str = ""
    component: str = ""
    file_purpose: str = ""
    # How many of the scan's N runs reported this finding, and that as a fraction.
    # With `scan.runs = 1` (the default) every finding is trivially 1 and 1.0; the
    # numbers only carry information once N > 1, and `summary["runs"]` says which.
    # NOTE: findings are keyed on the exact line, and the model drifts line numbers
    # between runs, so this is a LOWER BOUND on a finding's real stability.
    runs_seen: int = 1
    run_fraction: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def sort_key(self) -> tuple[int, str, int]:
        return (SEVERITY_ORDER.get(self.severity, 9), self.file, self.line)


@dataclass
class ScanResult:
    findings: list[ScanFinding] = field(default_factory=list)
    files_scanned: int = 0
    files_skipped: int = 0
