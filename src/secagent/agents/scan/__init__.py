"""UC4: LLM rule-based scan for memory & stability issues in C/C++ embedded code.

Unlike UC3 (IKOS, a formal static analyzer), this use case asks the local model to
review code against a *configurable, heuristic* rule set — distilled from the
NASA/JPL Power of Ten, MISRA C, CERT C, and BARR-C standards. The rules live in a YAML
file edited the same way as the review persona (``config/rules/*.yaml``), so teams can
add, remove, or re-scope rules without touching code.
"""

from .agent import scan_repo

__all__ = ["scan_repo"]
