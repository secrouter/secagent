# Running secagent on a project

A practical, end-to-end runbook for pointing secagent at a real codebase: index it, get
an architecture + per-function understanding (now including a C/C++ call map), and
generate a documentation site — driven by a local model. NASA
[cFS](https://github.com/nasa/cFS) is used as the worked example.

## 0. What you'll get

For a project you point secagent at:

- a **structure** map (components, languages, entry points) and an **IO map** (imports,
  endpoints, outbound calls, datastores);
- per-file **one-line summaries**;
- per-**function** signatures and a one-line *"what it does"* description;
- an inter-file **call map** (which file calls into which, via which functions) — for
  C/C++, from the clang AST;
- a Sphinx **documentation site** assembling all of the above (with diagrams).

## 1. Prerequisites

- **Python 3.11+.** On macOS the default `python3` is often 3.9 — use `python3.11` (or
  `make`, which selects a suitable interpreter). secagent isn't on PyPI yet; install from
  a clone.
- **A model endpoint.** Any OpenAI-compatible `/v1` endpoint — llama.cpp `llama-server`,
  vLLM, or LM Studio — serving a Gemma (or similar) model. No model is bundled.
- **For C/C++ projects:** the `clang` extra (bundles libclang). A
  `compile_commands.json` is optional but gives the most accurate results.
- **For the agentic workflow (optional):** the `pi` runtime (`npm i -g
  @earendil-works/pi-coding-agent`).

## 2. Install

```bash
git clone https://github.com/secrouter/secagent && cd secagent
make dev                       # editable install, all extras, Python 3.11+
# C/C++ analysis (accurate functions + call map) needs libclang:
python3.11 -m pip install -e ".[clang]"
# C#/.NET analysis (tree-sitter; no .NET SDK required):
python3.11 -m pip install -e ".[csharp]"
secagent doctor                  # self-check (OpenSSL/FIPS, deps, endpoint)
```

## 3. Point at your model

Copy `config/secagent.example.yaml` to `secagent.yaml` (or set `SECAGENT_*` env vars). The one
value you must get right is the model's real context window:

```yaml
llm:
  base_url: "http://192.168.1.50:11434/v1"   # your llama.cpp / vLLM / LM Studio
  model: "google/gemma-4-26b-a4b-qat"
  context_window: 16384                       # match your deployed variant
```

```bash
export SECAGENT_CONFIG=$PWD/secagent.yaml
secagent doctor --probe          # confirms the endpoint is reachable
```

> **Reasoning models** (which emit hidden reasoning before any answer) are handled —
> secagent's summary/description calls already reserve output-token headroom. If
> *everything* comes back empty, your endpoint's loaded context is too small; lower
> `context_window`.

## 4. Index the project

```bash
secagent index /path/to/project
```

This walks the tree, detects languages, extracts symbols, summarizes files, and builds
the structure/IO/call maps into a `.secagent/` store next to the project. It's
incremental — unchanged files (by SHA-256) are skipped on re-runs.

- **Version control is skipped by default** (`ignore_vcs: true`): the `.git` directory,
  submodule `.git` files, dotfiles, and `.github`/`.gitlab`. Source that merely *uses*
  Git (a `vcs/git` module, a GitLab client) is still indexed.
- Tune what's read with `affordances.max_file_bytes` and `ignore_globs`.

## 5. Query the affordances (the surface the agent drives)

These are read-only and return compact text/JSON; the `pi` agent calls them, and
they're handy standalone:

```bash
secagent affordance structure /path/to/project          # components, languages, entry points
secagent affordance io        /path/to/project          # imports, endpoints, calls, datastores
secagent affordance search    /path/to/project "table"  # rank files by relevance
secagent affordance summary   /path/to/project src/x.c  # one file's purpose + symbols
secagent affordance functions /path/to/project src/x.c  # functions: signature + description
secagent affordance calls     /path/to/project          # the inter-file call map
secagent affordance find-symbol /path/to/project Foo     # locate a function/type
secagent affordance context   /path/to/project "auth"   # budget-bounded context block
```

## 6. C/C++ projects — the clang setup

C/C++ functions and the call map come from the **clang AST** (libclang), which is far
more accurate than regex. Three things to know:

1. **Install the extra:** `pip install -e ".[clang]"`. If libclang is absent, secagent
   silently falls back to regex symbols / the LLM — indexing still succeeds, just with
   less detail.

2. **Best accuracy: a compile database.** Point secagent at a `compile_commands.json`:

   ```yaml
   affordances:
     clang_compile_db: "/path/to/project/build/compile_commands.json"
   ```

   Generate one with your build system — `cmake -DCMAKE_EXPORT_COMPILE_COMMANDS=ON`,
   or `bear -- make` for Make-based builds. (The `pi` agent can run these for you.)

3. **No build? Best-effort still works.** Without a compile DB, secagent parses each file
   with `-ferror-limit=0` and include directories auto-discovered from the **project
   root** — it climbs past submodule `.git` pointers, so even a component-scoped index
   resolves cross-component headers. Add SDK/system include dirs with
   `affordances.clang_extra_includes` if needed.

Worked example (cFS File Manager app, *unbuilt*, best-effort):

```bash
secagent index ~/cFS/apps/fm
secagent affordance functions ~/cFS/apps/fm fsw/src/fm_app.c
#  void FM_AppMain()          — enters the main loop processing Software Bus messages
#  CFE_Status_t FM_AppInit()  — initializes global data, telemetry, events, SB channels
secagent affordance calls ~/cFS/apps/fm fsw/src/fm_app.c
#  fsw/src/fm_app.c -> fsw/src/fm_child.c: FM_ChildInit
#  fsw/src/fm_app.c -> fsw/src/fm_dispatch.c: FM_TaskPipe
#  fsw/src/fm_app.c -> fsw/src/fm_table_utils.c: FM_TableInit
```

> **Scope tip.** Indexing a single component gives a tight, fast call map for that
> component (cross-component callees outside the scope simply aren't drawn). Index the
> whole repo for the project-wide map — slower, since every C/C++ file is parsed.

## 6a. C# / .NET projects

C# is handled the same way as C/C++, with **tree-sitter** as the AST backend instead of
clang — no .NET SDK and no built project required:

1. **Install the extra:** `pip install -e ".[csharp]"`. Without it, secagent falls back to
   regex symbols + the LLM (indexing still succeeds, just no call map).
2. **Index normally** — `secagent index /path/to/solution`. secagent parses every `.cs`
   file (skipping generated `*.Designer.cs` / `*.g.cs` / `AssemblyInfo.cs`), extracts
   methods, and builds the inter-file call map alongside any C/C++ in the repo.
3. `.csproj` / `.sln` files are recognized for project structure.

.NET IO signals are detected for the architecture/IO map: ASP.NET endpoints
(`[HttpGet("/x")]`, `app.MapPost("/x", …)`), config/env (`Environment.GetEnvironmentVariable`,
`Configuration["…"]`), and datastores (EF Core, SQL Server, Npgsql, Redis, MongoDB).

```bash
secagent index ~/MyService
secagent affordance functions ~/MyService Services/WidgetService.cs
secagent affordance calls ~/MyService          # e.g. WidgetService.cs -> Repo.cs: Count
```

> The C# call map is **syntactic** (tree-sitter), matching clang's best-effort fidelity:
> calls resolve by method name to the defining file. Overload-exact, namespace-aware
> resolution would need Roslyn (the .NET SDK) — not required here.

## 7. Generate the documentation site (UC1)

```bash
secagent docs build /path/to/project -o ./site
# open ./site/build/html/index.html
```

The site has: **Overview**, **Architecture** (with diagrams), **Components**, **Data
Flow & IO**, **Call Map**, and an **API Reference** listing each function with its
signature and the generated description.

- **Diagrams** render in pure Python by default (`diagrams.renderer: svg`) — no X
  server or browser. `chromium` / `drawio` backends are opt-in for pixel-faithful
  draw.io output.
- **Function descriptions** are LLM-generated during the build, capped by
  `affordances.max_function_docs` (default 120; one cached model call each). Scope the
  build to a component to keep it tractable on a large codebase; set `0` to skip them.

## 7a. Comparing models (evaluation)

The LLM summary/description cache is keyed by `(content, prompt, **model**)`, so you can
A/B different models cheaply: change `llm.model`, rebuild, and only the summaries
regenerate — everything else (file walk, and the C/C++ clang parse, which is cached by
file hash) is reused.

```bash
# config: llm.model = gemma-3-12b
secagent docs build /repo -o site-12b
# config: llm.model = gemma-3-27b
secagent docs build /repo -o site-27b        # summaries regenerated with 27b; clang reused
```

Each build writes a `summaries.json` / `summaries.md` manifest (file purposes + function
descriptions, tagged with the model) into its output dir — diff them to score the models:

```bash
diff <(jq -S . site-12b/summaries.json) <(jq -S . site-27b/summaries.json)
secagent affordance summaries /repo            # the current store's manifest as JSON
```

Re-running a model you've already tried is instant (cached). To force regeneration for
the *same* model (e.g. sampling at `temperature>0`), add `--refresh-summaries`.

## 8. Drive it with pi (agentic, optional)

Let the model loop over the repo using the affordance tools, then build docs:

```bash
npm install -g @earendil-works/pi-coding-agent
# ~/.pi/agent/models.json — note the api value:
#   { "providers": { "local-gemma": { "api": "openai-completions",
#       "baseUrl": "http://192.168.1.50:11434/v1", "apiKey": "not-needed",
#       "models": [ { "id": "google/gemma-4-26b-a4b-qat", "contextWindow": 32768 } ] } } }

SECAGENT_REPO=/path/to/project \
pi --extension pi/extensions/secagent.ts --provider local-gemma --model gemma-4-26b-a4b-qat
#   then, e.g.:  > Explore this repo with the secagent_* tools and summarize its
#                  architecture, then run /secagent-docs ./site
```

Run non-interactively with `pi -p "<prompt>"`. The extension registers
`secagent_structure`, `secagent_io_map`, `secagent_search`, `secagent_file_summary`,
`secagent_find_symbol`, `secagent_context`, `secagent_read_slice`, and the `/secagent-docs` /
`/secagent-review` commands.

> **Provider gotcha:** pi's `api` value must be `openai-completions` (not `openai`) for
> an OpenAI-compatible `/v1/chat` endpoint, or it errors with
> *"No API provider registered for api: openai"*.

## 9. Containers (optional)

```bash
make docker                                                    # FIPS UBI9 base + agent
docker compose -f docker/docker-compose.yml run --rm docs      # docs build in a container
```

The default image is lightweight (svg diagrams, no X server). See
[docs/fips.md](fips.md) for the FIPS posture and the opt-in faithful-rendering builds.

## 10. Quick reference — the settings that matter

| Setting | Default | What it does |
|---------|---------|--------------|
| `llm.context_window` | 131072 | Your model's real window (suits Gemma 3/4). Sizes the context budget *and* the empty-response retry. |
| `scan.max_files` | 0 | 0 = scan the whole project; set N (or `--max-files N`) for a quick pass. |
| `affordances.llm_summaries` | true | LLM file purposes + function descriptions (false = heuristic only). |
| `affordances.ignore_vcs` | true | Skip the project's own `.git`/`.github`/VCS metadata. |
| `affordances.clang_compile_db` | "" | `compile_commands.json` for accurate C/C++ (empty = best-effort). |
| `affordances.clang_extra_includes` | [] | Extra `-I` dirs for best-effort clang parsing. |
| `affordances.max_function_docs` | 120 | Cap on per-function LLM descriptions per docs build. |
| `diagrams.renderer` | svg | `svg` (default, no deps) / `chromium` / `drawio`. |
