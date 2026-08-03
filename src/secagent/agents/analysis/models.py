"""Data model for static-analysis findings."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# IKOS result status -> secagent severity. "error" is a definite defect; "warning" is a
# potential defect or an analysis that could not be proven safe.
SEVERITY_BY_STATUS = {
    "error": "high",
    "warning": "medium",
    "unreachable": "info",
    "note": "info",
    "ok": "ok",
    "safe": "ok",
}

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3, "ok": 4}


def severity_for(status: str) -> str:
    return SEVERITY_BY_STATUS.get((status or "").lower(), "info")


@dataclass
class Finding:
    checker: str          # IKOS analysis name, e.g. boa, dbz, nullity, uva, sio
    status: str           # ok | warning | error | unreachable
    severity: str         # high | medium | low | info | ok
    file: str
    line: int = 0
    column: int = 0
    message: str = ""
    # The function that CONTAINS `line`, resolved from the symbol index in `_enrich`.
    function: str = ""
    # The innermost caller on IKOS's path to this defect, from SARIF `stacks[]`. A
    # different fact from `function`, and it used to be written into it: `stacks[]` frames
    # are spelled "Call from <f>", so frames[0] names a CALLER of the containing function.
    # Ten of ten stacked findings on cFS `fm_cmd_utils.c` pointed a reader at a function
    # that does not contain the defect — worse than the empty field it replaced, because
    # an empty one is ignored and a wrong one is followed. The call path is genuinely
    # useful, so it is kept under a name that says what it is.
    called_from: str = ""
    # Enrichment added from the affordance store (component + file purpose).
    component: str = ""
    file_purpose: str = ""
    # Optional LLM triage.
    triage: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def sort_key(self) -> tuple[int, str, int]:
        return (SEVERITY_ORDER.get(self.severity, 5), self.file, self.line)
