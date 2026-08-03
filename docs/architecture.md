# Architecture

```{contents}
:local:
```

## Overview

```
repo ──► affordance engine ──► .secagent store (sqlite + json, sha256-addressed)
              │                      │
   structure / summaries /          ├─► docs agent  ──► Sphinx + Draw.io
   IO map / symbols                 ├─► review agent ──► GitLab MR comments
                                    └─► MCP server   ──► external MCP clients

pi (pi.dev) ── drives ──► secagent affordance CLI / extension / Skill
```

| Subsystem | Path | Role |
|-----------|------|------|
| Agent loop | external: **pi** (pi.dev) | the runtime; drives secagent via Skill/extension + bash |
| pi integration | `pi/` | TS extension (tools + slash commands), Skill, provider config |
| Affordance engine | `src/secagent/affordances/` | structure / summaries / IO map / symbols, store, retrieval, query CLI |
| LLM client | `src/secagent/llm/` | OpenAI-compatible client + Gemma tokenizer (for docs/review prose) |
| MCP (optional) | `src/secagent/mcp/` | stdio MCP server, GitLab REST v4 harness |
| Docs agent (UC1) | `src/secagent/agents/docs/` | outline → Draw.io → Sphinx |
| Review agent (UC100) | `src/secagent/agents/review/` | persona, triggers, webhook |
| FIPS surface | `src/secagent/security.py`, `doctor.py` | single hashing surface + self-check |

## Design principles

**pi owns the loop.** secagent deliberately does *not* implement an agentic loop;
that is pi's job. secagent provides high-signal, budget-bounded **observations** and the
two domain workflows.

**Affordances over raw source.** Every tool returns a compact string so the model's
context stays small. The retriever sizes assembled context to the deployed model's
window (see {doc}`affordances`).

**Deterministic where it matters.** Architecture diagrams are computed from the IO
graph, not drawn by the model. The model writes prose; structure is derived.

**Incremental + cached.** Indexing skips files whose SHA-256 is unchanged; LLM file
summaries are cached by content hash, so re-indexing is cheap.

**FIPS by construction.** A single hashing surface (SHA-256 only), system-OpenSSL TLS,
no bundled crypto, and a `secagent doctor` self-check. See {doc}`fips`.

## Data flow (UC1)

1. `index_repo` walks the repo, hashes files, extracts symbols + per-file summaries,
   and writes the store.
2. `build_project_map` and `build_io_map` derive the whole-repo views.
3. The docs agent turns those into a doc IA, generates `.drawio` diagrams from the IO
   map, renders them, and builds the Sphinx site.

## Data flow (UC100)

1. A webhook event (new MR, or a comment mentioning the bot) hits the FastAPI server.
2. The trigger fetches MR metadata + diffs via the GitLab harness.
3. The reviewer assembles a budget-bounded prompt: persona + affordance context for the
   changed files + a budgeted diff, then posts the review or in-thread reply.
