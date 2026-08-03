# Skill: secagent full analysis (UC0)

Drop into an unfamiliar project and produce a complete, structured analysis: bin its
components by language, run the right secagent tools on each, and synthesize a summary.
This composes the secagent affordances and use cases — use it before deep-diving so you
spend the model's context on findings, not discovery.

## Fast path (one command)

```
/secagent-analyze-all <out>        # default out: secagent-analysis
```

It indexes the repo, prints the per-language **plan**, and builds the docs +
per-file/function summaries into `<out>/docs`. Then run the recommended per-language
tools (below) and — by default — write a codebase **summary with diagrams** (Step 3;
add `--no-summary` to skip it). Equivalent manual steps if you're not using the
extension:

```bash
secagent index <repo>
secagent affordance plan <repo>                       # the binning + tool plan (JSON)
secagent docs build <repo> -o <out>/docs              # architecture + summaries site
```

## Step 1 — Orient and bin by language

`secagent affordance plan <repo>` (tool: `secagent_plan`) returns, deterministically:

- `languages` — file counts per language across the repo
- `bins` — each language → the components written in it
- `per_language_tools` — the secagent commands to run on each language
- `entrypoints`, `always_run`

Don't infer the toolchain yourself — the plan tells you what to run.

## Step 2 — Run the per-language tools

Run what the plan lists for each language present. The mapping it encodes:

| Language | Run | Gives you |
|----------|-----|-----------|
| C / C++ | `secagent affordance calls`; `secagent scan -o <out>/scan`; `secagent analyze run <file>` (optional, IKOS) | clang call map; memory/stability findings (Power of Ten/MISRA/CERT); static-analysis findings |
| C# | `secagent analyze deep <repo>` (Roslyn — needs `make analyzer-dotnet`); else `secagent affordance calls` | qualified call map + type/inheritance graph; (tree-sitter call map as fallback) |
| Python / JS / TS / Go / Java / Rust / … | `secagent affordance summaries`; `secagent affordance calls` | file purposes + function descriptions; call map where available |

Always also: `secagent affordance structure` (map), `secagent affordance io` (wiring).

If a heavy backend isn't available (`analyze deep` without the image, `analyze run`
without IKOS), note it and use the light call map / summaries instead — never block.

## Step 3 — Synthesize the summary + diagrams (default)

By **default**, finish UC0 by writing a codebase summary that **embeds the diagrams secagent
already generated**. This step is **optional** but on by default: skip it with
`/secagent-analyze-all <out> --no-summary`, or when the user only wants the raw tool output.

**Don't hand-author diagrams (no Mermaid/ASCII).** The docs build (an `always_run` step)
produces **Draw.io diagrams deterministically from the IO map** — accurate by construction.
After it runs they're at:

- `<out>/docs/source/_diagrams/components.svg` — **architecture** (components + imports);
  editable source `components.drawio`.
- `<out>/docs/source/_diagrams/system_io.svg` — **data flow / IO** (endpoints, outbound,
  datastores, and message brokers: Kafka/MQTT/RabbitMQ/ZeroMQ/NATS/…); source `system_io.drawio`.
- The **Call Map** page of the docs site — inter-file call edges (`secagent affordance calls`).

Write `ANALYSIS.md` at the repo root from the docs site (`<out>/docs`), the summaries
(`<out>/docs/summaries.md`), and the per-language tool outputs. **Embed / link those
diagrams** and explain each in prose grounded in `secagent affordance structure`/`io`/`calls`.
Include only the views that say something about this project (a library with no IO needs no
data-flow diagram). Link the editable `.drawio` for anyone who wants to adjust a diagram.

Template:

```markdown
# <project> — analysis

## Overview
<2–3 sentences: what it is and does, from the docs Overview>

## Architecture
![Architecture](<out>/docs/source/_diagrams/components.svg)
<what the component graph shows; key entrypoints>

## Components by language
<for each language bin: the components, with each component's one-line purpose>

## Data flow
![Data flow](<out>/docs/source/_diagrams/system_io.svg)
<the IO map in prose: imports / endpoints / outbound / datastores / messaging>

## Per-language findings
- **C/C++**: call-map highlights (see the Call Map page); scan findings (severity + file:line); IKOS findings
- **C#**: type hierarchy + qualified call highlights
- **<other>**: notable summaries

## Risks & follow-ups
<missing tests, scan/analysis findings to address, unclear areas worth a deeper look>
```

Keep it grounded in tool output — cite `file:line` and component names; don't invent
behavior. Point the reader at the generated docs site for detail.
