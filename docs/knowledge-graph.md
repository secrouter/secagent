# The code knowledge graph

`secagent kg build <repo>` projects the affordance store (see
[The affordance engine](affordances.md)) into a small, queryable graph — entities
(`FILE`, `SYMBOL`, `COMPONENT`, `EXTERNAL`) and relations (`defined_in`, `calls`,
`may_call`, `inherits`, `implements`, plus the IO predicates: `imports`, `exposes`,
`calls_http`, `reads_env`, ...). It is **deterministic**: no LLM runs in the projection
step, so the graph is a faithful, re-runnable mirror of the code and a cheap model can
answer over it (`secagent kg recall <repo> "<question>"`).

## Two extraction tiers, per language

Symbols, files, types, and IO edges come straight out of the affordance store. **Call
edges** are different: most affordance backends are syntactic (name-based) and don't
themselves resolve `obj.method()` to the receiver's real type, so the KG build step adds
a second, call-specific extraction pass — chosen per language, LIGHT by default, HEAVY
under `secagent kg build --deep`.

| Language | Light (default, no extra toolchain) | Heavy (`--deep` / an affordance semantic backend) |
|---|---|---|
| Python | `secagent.kg.pycalls` — `ast`, name-resolved | `secagent.kg.pyjedi` — [jedi](https://github.com/davidhalter/jedi) infers the receiver's type, so `obj.method()` resolves to its real definition |
| Go | `secagent.kg.treesitter_calls` — tree-sitter, name-resolved | `secagent.kg.gocalls_semantic` — a Go type-checker frontend (e.g. `go-callgraph`/`golang.org/x/tools/go/callgraph`) |
| TypeScript / JavaScript | `secagent.kg.treesitter_calls` — tree-sitter, name-resolved | `secagent.kg.tscalls_semantic` — the TypeScript compiler (`tsc`) checker API |
| C / C++ | libclang, best-effort AST (`affordances.clang_ast`) — name-resolved | *(later phase — see [heavy-analysis-pipeline](design/heavy-analysis-pipeline.md); no compiled semantic C/C++ backend ships yet)* |
| C# | tree-sitter (`affordances.csharp_ast`) — name-resolved | Roslyn/MSBuildWorkspace, in an opt-in container (`secagent analyze deep`) — resolves by qualified symbol and tags virtual/interface dispatch |
| Rust | tree-sitter (`affordances.rust_ast`) — name-resolved | rust-analyzer via SCIP, in an opt-in container (`secagent analyze deep`) — resolves by qualified symbol and tags trait dispatch |

Python/Go/TS/JS's heavy extractors live **in the KG layer** (`secagent.kg.*`) because the
affordance store never extracts their calls at all (light or heavy) — see
`secagent/kg/extractors.py`'s module docstring for the exact naming convention a new
heavy module must follow to be picked up automatically.

C/C++/C#/Rust are the opposite: their light AND heavy extraction both happen **in the
affordance layer** (`secagent.affordances.*`), before the KG is ever built —
`secagent.kg.extractors.run_extractors` detects that a language's `calls` edges are
already resolved (any language whose files appear as the caller side of an affordance
call edge; see `_affordance_resolved_languages`) and skips running a redundant
name-based KG-level pass for it.

## `--deep` selection

`secagent kg build --deep <repo>` does two independent things, one per tier:

1. **Affordance heavy backends** (C#/Rust) are opt-in *containers* invoked separately,
   via `secagent analyze deep <repo>` (or `analyze deep --ingest <report.json>` with no
   container at all — see below). Their resolved `calls`/`types` rows land in the
   affordance store *before* `kg build` runs, so `kg build` (deep or not) picks them up
   automatically through `project_affordances`.
2. **KG-level heavy extractors** (Python/Go/TS/JS) run only when `--deep` is passed to
   `kg build` itself, and only if their runtime dependency
   (jedi / a Go toolchain / `tsc`) is actually importable and working — checked by each
   module's `..._available() -> bool`, not just import success. Absent, `kg build --deep`
   silently falls back to that language's light extractor; nothing errors.

Either way, a language whose heavy path isn't available (or wasn't requested) still gets
its light pass — `--deep` only ever *upgrades* precision, it never removes coverage.

## The `edge_kind` → predicate mapping

A semantic backend can tell a plain call from dispatch through a vtable or an interface;
a syntactic one never can. The affordance `CallEdge.edge_kind` (`direct | virtual |
interface`) carries that distinction into the KG as an honest predicate
(`kg/project.py`, `_CALL_PREDICATE`):

```
direct    -> calls      # an unconditional call to exactly this definition
virtual   -> may_call    # dispatches through one of possibly several overriders
interface -> may_call    # dispatches through one of possibly several implementers
```

`may_call` is deliberately less specific than `calls`: the edge is real, but the exact
runtime target isn't statically knowable without the concrete receiver type, so the
graph doesn't claim more precision than the analysis actually has.

## The `secagent-analysis/v1` contract

C#'s and Rust's heavy backends (and any future compiled backend, including C/C++'s
eventual one) all emit one normalized JSON shape — functions, types, and calls, each
call carrying `edge_kind` — so `secagent.affordances.analysis.ingest_report` is the
single, backend-agnostic ingest path for all of them. It **merges** into the existing
call map rather than replacing it (a heavy run that only partially resolves — e.g. an
unrestored C# project — must never delete edges the light pass already found), and
qualified names are stored as the entity's `qualifier` (its defining file becomes the
KG entity's `source`), so two same-named symbols in different files/types stay distinct
nodes.

`secagent analyze deep --ingest <report.json>` runs the whole store-enrichment path with
**no container at all** — the testable seam for the contract, and how the projector
boundary is exercised in CI without docker (see `tests/test_kg_native_calls.py` and
`tests/test_heavy_analysis.py`).

See [Design: heavy (compiled) C/C++ and C# analysis pipeline](design/heavy-analysis-pipeline.md)
for the full backend architecture (container sandboxing, offline/FIPS posture,
per-language rollout phase).
