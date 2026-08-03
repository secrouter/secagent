"""UC5: automatic test generation.

Walks the project (via the UC1 affordance store) and asks the local model to draft
two kinds of tests:

* **Unit tests** — one test module per source file, exercising its public symbols.
* **Functional / component I/O tests** — driven by the IO map: each component's
  exposed endpoints, outbound calls, and datastore usage become input/output tests.

Generated tests are written to a **separate top-level folder** (default
``secagent-tests/``) so they never mix with the project's own structure. UC5 relies on
UC1's output and recommends running it first for richer context.
"""

from .agent import generate_tests

__all__ = ["generate_tests"]
