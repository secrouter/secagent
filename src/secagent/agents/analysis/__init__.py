"""UC3: C/C++ static analysis via IKOS (NASA-SW-VnV/ikos).

Runs IKOS — an abstract-interpretation static analyzer that detects runtime errors in
C/C++ (buffer overflows, null dereferences, integer overflows, division by zero,
uninitialized reads, …) — then enriches each finding with secagent affordances (the
component and file purpose it touches), optionally triages it with the local model,
and renders a budget-bounded Markdown + JSON report.

IKOS itself is an optional, heavyweight native toolchain; when its binary is absent
the agent runs in **ingest mode** against a pre-produced IKOS report, so the use case
works (and tests) anywhere.
"""

from .agent import analyze_repo

__all__ = ["analyze_repo"]
