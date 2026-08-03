"""UC1: deep-dive documentation agent.

Produces a comprehensive Sphinx documentation set with Draw.io architecture
diagrams. Diagrams are generated *deterministically* from the affordance IO map, so
they are accurate by construction; the local model is only asked to write prose,
which keeps it robust even on small Gemma variants.
"""

from .agent import build_docs

__all__ = ["build_docs"]
