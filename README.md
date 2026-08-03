# secagent

**Containerized pi-agents for local (Gemma) models — FIPS-compatible.**

secagent pairs the [**pi** coding agent](https://pi.dev) (the agentic loop) with a
context-frugal toolset built for *local* models (the Gemma family). **pi is the
runtime** — it owns the loop, tools (read/write/edit/bash), sessions, and provider
selection. **secagent is what pi drives**: an **affordance engine** that pre-computes
compact, content-addressed representations of a codebase — project-structure map,
per-file summaries, a service/component IO map (imports, endpoints, outbound calls,
datastores, message queues), a symbol index, and an inter-file call map (C/C++ via
clang, C# and Rust via tree-sitter, with optional fully-resolved backends) — so the agent works from a minimal,
*budget-bounded* context instead of raw source. (See [`pi/README.md`](pi/README.md) for
the integration.)

The toolset is **portable** and **endpoint-agnostic**: pi and secagent both point at any
OpenAI-compatible endpoint (llama.cpp `llama-server` or vLLM). No model server is
bundled.

The use cases, all built on the same affordance engine:

0. **Full project analysis** (`/secagent-analyze-all`) — drop pi into an unfamiliar
   project and have it bin the components by language, run the right secagent tools on
   each (call map, scan, heavy analysis, …), and synthesize a summary. Orchestration
   over the others, driven by a pi Skill + slash commands. See
   [docs/full-analysis.md](docs/full-analysis.md).
1. **Docs deep-dive** (`secagent docs`) — pi loops over a codebase using the affordance
   tools, then builds a comprehensive Sphinx site with Draw.io architecture diagrams.
   Diagrams are derived *deterministically from the IO map* (accurate by
   construction); the model only writes prose, so it stays robust on small Gemma
   variants.
2. **GitLab MR review** (`secagent review`) — reviews new merge requests, posts an
   initial comment, and replies in-thread when @-mentioned. Behaviour is steered by
   an editable persona (alignment + verbosity), and it reuses the affordance store to
   reason about cross-component impact, not just the diff.
3. **C/C++ static analysis** (`secagent analyze`) — runs [IKOS](https://github.com/NASA-SW-VnV/ikos)
   (NASA's abstract-interpretation analyzer), enriches each finding with the owning
   component + file purpose, optionally triages it with the local model, and writes a
   Markdown + JSON report. Runs IKOS directly or ingests a report produced elsewhere.
4. **Memory/stability scan** (`secagent scan`) — the local model reviews C/C++ against a
   **configurable, heuristic rule set** for embedded systems (distilled from NASA/JPL
   Power of Ten, MISRA, CERT C, BARR-C). Rules live in `config/rules/*.yaml`, edited
   the same way as the review persona.
5. **Auto test generation** (`secagent testgen`) — walks the structure (UC1 output) and
   drafts **unit tests** (per file) and **functional component I/O tests** (from the
   IO map) into a separate top-level folder. Run UC1 first.

## Why "affordances"?

A Gemma-2 model has an 8k window; even Gemma-3 (128k) reasons better with less, more
structured input. secagent spends cheap, cached, deterministic passes turning a repo
into high-signal artifacts, then a budget-aware retriever assembles only what a task
needs. The model sees summaries, relevant symbols, and IO edges — not whole files.

```
repo ──► affordance engine ──► .secagent store (sqlite + json, sha256-addressed)
              │                      │
   structure / summaries /          ├─► docs agent  ──► Sphinx + Draw.io
   IO map / symbols                 ├─► review agent ──► GitLab MR comments
                                    └─► MCP server   ──► external MCP clients
```

## Install

**Requires Python 3.11+.** secagent isn't on PyPI yet — install from a clone. `make dev`
picks a suitable interpreter (handy since the default `python3` on macOS is often an
older system build):

```bash
make dev    # editable install with all extras
```

Or point pip at Python 3.11+ yourself (a virtualenv keeps it isolated):

```bash
python3.11 -m venv .venv && . .venv/bin/activate
pip install -e ".[docs,review,tokenizer,clang,csharp,dev]"
```

Extras: `docs` (Sphinx + sphinxcontrib-drawio), `review` (FastAPI webhook server),
`tokenizer` (precise Gemma token counts; falls back to a heuristic when absent),
`clang` (accurate C/C++ functions + call map via libclang), `rust` (Rust functions +
call map via tree-sitter), `csharp` (C# functions +
call map via tree-sitter), `dev` (ruff/mypy/pytest). All optional — secagent falls back
to regex symbols when an extra is absent.

## Configure

Copy `config/secagent.example.yaml`, or use `SECAGENT_*` environment variables. The one
value you must match to your deployment is the model's real context window:

```yaml
llm:
  base_url: "http://gemma-host:8000/v1"   # llama.cpp or vLLM
  model: "gemma-3-12b-it"
  context_window: 8192                      # Gemma2 8k / G3-1B 32k / G3-4B+ 128k
```

```bash
# llama.cpp:  llama-server -m gemma-3-12b-it-Q4_K_M.gguf --host 0.0.0.0 --port 8000
# vLLM:       vllm serve google/gemma-3-12b-it --port 8000
```

## Use

Run via pi (recommended — see [`pi/README.md`](pi/README.md)) or directly:

```bash
secagent doctor                                  # FIPS + dependency self-check
secagent index path/to/repo                      # build the affordance store
secagent index path/to/repo --no-llm             # ...structure only: much faster,
                                               #    no per-file LLM summaries
secagent affordance cache path/to/repo           # LLM cache size; --prune N / --clear
secagent mcp affordances                         # serve the affordances over MCP
                                               #   (Kilo Code / OpenCode / any MCP
                                               #    client — see docs/integrations.md)

# Affordance queries — the surface pi drives via bash (also handy standalone):
secagent affordance structure path/to/repo       # components, languages, entrypoints
secagent affordance io path/to/repo              # imports, endpoints, calls, datastores, message queues
secagent affordance plan path/to/repo            # UC0: components binned by language + tools
secagent affordance search path/to/repo "auth"   # rank files by relevance (JSON)
secagent affordance summary path/to/repo a/b.py  # one file's purpose + symbols (JSON)
secagent affordance functions path/to/repo a/b.c # functions: signature + description (JSON)
secagent affordance calls path/to/repo           # the inter-file call map
secagent affordance summaries path/to/repo       # per-model manifest of generated summaries
secagent affordance find-symbol path/to/repo foo # locate a function/class (JSON)
secagent affordance context path/to/repo "auth"  # budget-bounded context block
secagent affordance slice path/to/repo a/b.py --start 1 --end 40

# Use cases:
secagent docs build path/to/repo -o ./site       # UC1: Sphinx + Draw.io docs
secagent analyze deep path/to/repo               # heavy (compiled) C# analysis (Roslyn)
secagent review mr group/project 42 --dry-run    # UC100: print a review for MR !42
secagent review serve --port 8080                # UC100: GitLab webhook receiver

# Optional: expose the same tools over MCP (pi has no built-in MCP; Skills/extension
# are the primary path, but these are handy for other MCP clients):
secagent mcp affordances path/to/repo
secagent mcp gitlab
```

### Tuning the reviewer (alignment & verbosity)

Edit `config/alignment/default.yaml` (or point `persona.profile` at another file).
Profiles are reloaded per review — no restart needed. See `security-strict.yaml` for
a stricter example. Knobs: `alignment` (stance), `verbosity` (terse/normal/detailed),
`focus_areas`, `tone`, and comment `limits`.

### GitLab webhook

Point a GitLab **Merge request events** + **Comments** webhook at
`https://<host>:8080/webhook`, set the secret token to match
`SECAGENT_GITLAB__WEBHOOK_SECRET`. New MRs get an initial review; comments that mention
the bot (`@secagent-bot`) get an in-thread reply. For air-gapped instances without
webhook delivery, set `gitlab.poll_interval_s` and use the polling fallback
(`secagent review poll <project>`). For the full loop + GitLab infrastructure setup
(bot user, token scopes, webhook config), see [docs/gitlab-watch.md](docs/gitlab-watch.md).

## Containers

```bash
make docker            # builds secagent-base + secagent-agent
docker compose -f docker/docker-compose.yml run --rm pi    # interactive deep-dive
docker compose -f docker/docker-compose.yml up review      # GitLab review webhook
docker compose -f docker/docker-compose.yml run --rm docs  # deterministic docs build

# Opt-in heavy-toolchain images (not part of `make docker`):
make docker-analysis                                       # UC3 IKOS/LLVM static analysis
make analyzer-dotnet                                       # C# heavy analysis (Roslyn/.NET SDK)
docker compose -f docker/docker-compose.yml --profile analysis run --rm analysis \
    analyze run /repo /repo/src/foo.c -o /out
```

One agent image carries both runtimes — Node (pi) and Python (secagent) — on a
FIPS-capable UBI9 base. Diagrams render in pure Python by default (no X server or
browser); pass `--build-arg DIAGRAM_BACKEND=chromium` (headless Chromium) or
`=drawio` (drawio-desktop + Xvfb) for pixel-faithful draw.io rendering. See
[docs/fips.md](docs/fips.md).

## Architecture

| Subsystem | Path | Role |
|-----------|------|------|
| Agent loop | external: **pi** (pi.dev) | the runtime; drives secagent via Skills/extension + bash |
| pi integration | `pi/` | TS extension (affordance tools + slash commands), Skill, provider config |
| Affordance engine | `src/secagent/affordances/` | structure/summaries/IO/symbols, store, retrieval, query CLI |
| LLM client | `src/secagent/llm/` | OpenAI-compatible client + Gemma tokenizer (for docs/review prose) |
| MCP (optional) | `src/secagent/mcp/` | stdio MCP server, GitLab REST v4 harness |
| Docs agent (UC1) | `src/secagent/agents/docs/` | outline → Draw.io → Sphinx |
| Review agent (UC100) | `src/secagent/agents/review/` | persona, triggers, webhook |
| Analysis agent (UC3) | `src/secagent/agents/analysis/` | IKOS run/ingest → enrich → triage → report |
| Scan agent (UC4) | `src/secagent/agents/scan/` | configurable rules → LLM review → report |
| Testgen agent (UC5) | `src/secagent/agents/testgen/` | structure + IO map → unit + functional tests |
| Heavy analysis | `tools/secagent-roslyn/`, `src/secagent/affordances/{analysis,heavy}.py` | optional *compiled* backends (C# Roslyn now; C/C++ clang build later) → `secagent-analysis/v1` contract → store. See [docs/design/heavy-analysis-pipeline.md](docs/design/heavy-analysis-pipeline.md) |

## Development

```bash
make verify   # ruff + mypy + pytest + secagent doctor
```

Tests run fully offline against a mock OpenAI endpoint and a mock GitLab API; real
endpoints are wired and documented but not required to develop.

## License

Apache-2.0.
