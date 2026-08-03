"""secagent — context-frugal toolset that the pi (pi.dev) agent runtime drives.

The agentic *loop* is provided by **pi** — the open-source TypeScript coding-agent
runtime — which calls secagent via its Skills/extension + bash tools (see ``pi/``).
secagent supplies the local-model affordances and the two use cases:

* ``secagent.affordances`` — the context-reduction engine. It turns a repository
  into compact, content-addressed artifacts (structure map, file summaries, an
  IO map between components, a symbol index) so that small/local models can work
  from a minimal, budget-bounded context instead of raw source. Exposed to pi via
  the ``secagent affordance`` CLI (``secagent.affordances.queries``).
* ``secagent.llm`` — a portable OpenAI-compatible client (used by the Python docs and
  review agents for prose/summaries), plus Gemma-aware token budgeting.
* ``secagent.agents.docs`` — use case 1: deep-dive Sphinx docs with Draw.io diagrams.
* ``secagent.agents.review`` — use case 2: GitLab merge-request review.
* ``secagent.mcp`` — optional: exposes the affordance/GitLab tools over MCP.

Everything is designed to be FIPS-compatible: hashing uses SHA-256 only, TLS comes
from the system OpenSSL, and no non-validated crypto is bundled.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
