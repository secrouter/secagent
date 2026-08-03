# Full project analysis (UC0)

Drop pi into an unfamiliar project and have it produce a complete, structured analysis:
**bin the components by language, run the right secagent tools on each, then synthesize a
summary with diagrams.** UC0 doesn't add a new engine — it orchestrates the affordances
and the other use cases as a set of pi action templates (a Skill + slash commands).

## TL;DR

```bash
# in pi, with the secagent extension loaded and SECAGENT_REPO set:
/secagent-analyze-all ./out
```

That indexes the repo, prints the per-language plan, and builds the docs +
per-file/function summaries into `./out/docs`. Then pi runs the recommended per-language
tools and — by default — writes `ANALYSIS.md`, a codebase summary **with diagrams**
(`--no-summary` to skip). Standalone (no pi), the same steps are:

```bash
secagent index <repo>
secagent affordance plan <repo>                 # the binning + tool plan (JSON)
secagent docs build <repo> -o ./out/docs        # architecture + summaries site
```

## 1. Orient and bin by language

`secagent affordance plan <repo>` (pi tool: `secagent_plan`) returns, deterministically:

- `languages` — file counts per language
- `bins` — each language → the components written in it
- `per_language_tools` — the secagent commands to run for each language
- `entrypoints`, `always_run`

The agent doesn't infer the toolchain — the plan tells it what to run. Example:

```json
{
  "languages": { "C": 412, "C#": 37, "Python": 8 },
  "bins": { "C": ["apps/fm", "cfe"], "C#": ["src/Service"], "Python": ["tools"] },
  "per_language_tools": {
    "C":  ["secagent affordance calls <repo>", "secagent scan <repo> -o <out>/scan", "..."],
    "C#": ["secagent analyze deep <repo>", "secagent affordance calls <repo>"]
  },
  "always_run": ["secagent index <repo>", "secagent affordance io <repo>", "secagent docs build ..."]
}
```

## 2. Run the per-language tools

| Language | Run | Gives you |
|----------|-----|-----------|
| **C / C++** | `secagent affordance calls`; `secagent scan -o <out>/scan`; `secagent analyze run <file>` (IKOS, optional) | clang inter-file call map; memory/stability findings (Power of Ten/MISRA/CERT); static-analysis findings |
| **C#** | `secagent analyze deep <repo>` (Roslyn — needs `make analyzer-dotnet`); else `secagent affordance calls` | qualified call map + type/inheritance graph; (tree-sitter call map as the fallback) |
| **Python / JS / TS / Go / Java / Rust / …** | `secagent affordance summaries`; `secagent affordance calls` | file purposes + function descriptions; call map where available |

Always also: `secagent affordance structure` (the map) and `secagent affordance io` (the
wiring — imports, endpoints, outbound calls, datastores, and **message queues / brokers**
like Kafka, MQTT, RabbitMQ, ZeroMQ, NATS, …).

If a heavy backend isn't available — `analyze deep` without the
`secagent-analyzer-dotnet` image, or `analyze run` without IKOS — note it and use the
light call map / summaries instead. UC0 never blocks on an optional toolchain.

## 3. The summary + diagrams (default)

You get three layers of output:

1. **The docs site** (`<out>/docs`) — Overview, Architecture (with diagrams), Components,
   **Data Flow & IO**, **Call Map**, and an **API Reference** with per-function
   descriptions. Open `<out>/docs/build/html/index.html`.
2. **The summaries manifest** (`<out>/docs/summaries.md` / `.json`) — every generated
   file purpose and function description, tagged with the model.
3. **`ANALYSIS.md`** — by **default**, pi finishes UC0 by writing a codebase summary at
   the repo root from the above plus the per-language tool output, **embedding the Draw.io
   diagrams secagent already generated** in the docs build. pi does *not* hand-author
   diagrams: they're produced **deterministically from the IO map** (accurate by
   construction) and live next to the site —
   `<out>/docs/source/_diagrams/components.svg` (architecture: components + imports) and
   `system_io.svg` (data flow: endpoints, outbound, datastores, message brokers), each with
   an editable `.drawio` source. pi embeds the views that fit the project and links the
   Architecture / Data Flow & IO / **Call Map** pages.

   ```markdown
   # <project> — analysis
   ## Overview
   ## Architecture           ![](<out>/docs/source/_diagrams/components.svg)
   ## Components by language
   ## Data flow              ![](<out>/docs/source/_diagrams/system_io.svg)
   ## Per-language findings   (C/C++ call map + scan/IKOS; C# types + qualified calls)
   ## Risks & follow-ups
   ```

   This step is **optional**: pass `--no-summary` (`/secagent-analyze-all <out> --no-summary`)
   — or ask pi for the raw tool output only — to skip the summary + diagrams.

Keep it grounded in tool output — cite `file:line` and component names; point the reader
at the generated docs site for detail.

## Setup

UC0 needs pi (for the agentic path) and secagent, plus the optional extras for the heavy
backends:

```bash
pip install -e ".[docs,review,tokenizer,clang,csharp]"   # clang = C/C++ AST; csharp = tree-sitter
make analyzer-dotnet                                      # optional: C# Roslyn heavy backend image
pi --extension pi/extensions/secagent.ts --provider local-gemma --model <model>
```

The pi side is a Skill + slash commands: `pi/skills/secagent-analysis/SKILL.md` is the
playbook; the extension registers `secagent_plan` and `/secagent-analyze-all`, `/secagent-plan`.
See {doc}`pi` for loading the extension and {doc}`running-on-a-project` for the
per-language details.
