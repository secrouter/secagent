---
orphan: true
---

# Design: heavy (compiled) C/C++ and C# analysis pipeline

Status: **approved plan** — phased delivery, C# first.

## Goal

Add an optional, high-fidelity analysis path that **actually compiles** a project to get
fully-resolved, semantic symbol/call/type information — without compromising the current
zero-toolchain experience. The heavy toolchains live entirely in **optional container
images** that secagent invokes when present and falls back from when absent.

## Principle: light stays the default

| Path | C/C++ | C# | Toolchain | Default |
|------|-------|----|-----------|---------|
| Light (today) | libclang best-effort | tree-sitter | none | ✅ |
| Heavy (this design) | real build → clang index | Roslyn / MSBuild | clang+LLVM / .NET SDK | opt-in |

The core never hard-depends on a multi-GB toolchain. The fallback ladder per language:

```
heavy (compiled, semantic)  →  light (syntactic)  →  regex  →  LLM-only
```

## Locked decisions

1. **Sequencing:** define the contract, then ship the **C# Roslyn** backend first
   (single well-supported tool; proves the seam before the C/C++ build-system matrix).
2. **Network:** **offline-only** builds. No network during analysis; dependencies must
   be pre-restored/vendored. Air-gapped- and FIPS-friendly; no untrusted build/restore
   logic reaches the network.
3. **Depth:** semantic **call graph + symbols + type/inheritance graph** (incl.
   virtual/override/interface dispatch edges). Static-analysis findings are **out of
   scope here** — see [Deferred](#deferred-static-analysis).

## The contract: `secagent-analysis/v1`

Every backend (Roslyn now, clang later) emits the same JSON, so ingest, store, and docs
are written once and are backend-agnostic. This extends the existing UC3 pattern of
"run a heavy tool → ingest a normalized report" (`secagent analyze ingest`).

```javascript
{
  "schema": "secagent-analysis/v1",
  "language": "C#",                 // or "C++"
  "backend": "roslyn-msbuild",      // or "clang-build"
  "functions": [
    { "name": "Get", "qualified_name": "Demo.WidgetsController.Get",
      "signature": "IActionResult Get()", "file": "Controllers/WidgetsController.cs",
      "line": 12, "kind": "method", "owning_type": "Demo.WidgetsController" }
  ],
  "types": [
    { "qualified_name": "Demo.WidgetsController", "kind": "class",
      "bases": ["Microsoft.AspNetCore.Mvc.ControllerBase"],
      "interfaces": [], "file": "Controllers/WidgetsController.cs", "line": 8 }
  ],
  "calls": [
    { "caller_qualified": "Demo.WidgetsController.Get",
      "callee_qualified": "Demo.Repo.Count", "callee_file": "Repo.cs",
      "line": 14, "edge_kind": "direct" }     // direct | virtual | interface
  ],
  "build": { "system": "msbuild", "restored": true,
             "toolchain": { "dotnet": "9.0.x" }, "offline": true }
}
```

## What "full weight" buys over the light path

| Dimension | Light | Heavy |
|-----------|-------|-------|
| Call resolution | by **name** (cross-file collisions) | by **qualified symbol** (exact) |
| Macros / templates / generics | unexpanded | expanded / instantiated |
| Virtual / interface dispatch | missed | edges tagged `edge_kind` |
| Cross-project / cross-TU | partial | complete |
| Types / inheritance | none | full hierarchy |

## C# backend — Roslyn / MSBuildWorkspace

A small .NET console tool, `secagent-roslyn`:

1. `MSBuildWorkspace.OpenSolutionAsync(<.sln>)` (or `OpenProjectAsync` for a loose
   `.csproj`). **No `dotnet restore`** (offline).
2. Per `Project` → `Compilation`; per `Document` → `SyntaxTree` + `SemanticModel`.
3. **functions/types** via `GetDeclaredSymbol` — qualified names, owning type, bases
   (`INamedTypeSymbol.BaseType`), interfaces (`.Interfaces`).
4. **calls** via `GetSymbolInfo(InvocationExpression)` → resolved `IMethodSymbol`;
   `edge_kind` = `virtual` (symbol virtual/abstract/override), `interface` (declared on
   an interface), else `direct`. Callee file from `symbol.Locations`.
5. Emit the contract JSON.

**Offline caveat (designed-around):** MSBuildWorkspace needs the project already
**restored** (`obj/project.assets.json` + a NuGet package cache) to resolve
cross-assembly references. Offline heavy mode therefore assumes the repo was
restored/built on the host first, or a vendored `~/.nuget/packages` is mounted. If it
isn't, Roslyn degrades to partial resolution — the report sets `"restored": false` and
emits whatever resolved (never worse than the light path).

## Rust backend — rust-analyzer / SCIP (delivered)

Image `secagent-analyzer-rust` (`make analyzer-rust`, `docker/analyzer-rust.Dockerfile`).
The Rust toolchain ships in the image so rust-analyzer can load crate metadata; the
target project is **not** built (SCIP indexing is analysis, not a full build). Pipeline:

1. `rust-analyzer scip .` over the crate/workspace → a resolved SCIP index (`index.scip`).
2. `secagent-rust-analyzer` (a small Rust binary, `tools/secagent-rust-analyzer`, using the
   `scip` crate) converts the index to the contract:
   - **functions/types** from Definition occurrences. rust-analyzer encodes impl blocks as
     `mod/impl#[Self][Trait]method().` — an `impl` Type descriptor followed by the Self
     type and (for a trait impl) the trait as TypeParameter descriptors — so an impl
     method normalizes to `mod::Self::method` and the trait impl yields Self's
     `interfaces` entry directly off the symbol (no relationship lookup needed).
   - **calls** by attributing each reference-to-a-callable to the enclosing function
     (`enclosing_range` when present, else the nearest preceding definition in the file).
   - Emits `secagent-analysis/v1` on stdout (progress on stderr).

**Offline caveat:** rust-analyzer resolves dependencies from the cargo cache; offline mode
therefore assumes deps were pre-fetched (`cargo fetch`/vendored). A std-only crate needs
nothing. Unresolved deps degrade to partial resolution — never worse than the light path.

## C/C++ backend — real build → clang semantic index (later phase)

Container with full clang/LLVM + cmake/ninja/make/bear. Pipeline: **detect build
system** (CMakeLists → cmake; Makefile → `bear -- make`; meson; autotools) → configure
+ build (offline) → `compile_commands.json` → semantic extraction. Extraction options,
cheapest first:

- **clangd-indexer** over `compile_commands.json` — cross-TU symbols + references, no
  custom C++ to maintain (recommended start).
- **LibTooling tool** or **LLVM-IR call graph** (`clang -emit-llvm` → `llvm-link` →
  `opt -print-callgraph`) — fully resolved, macro/template-aware, virtual dispatch.

Emits the same contract. Deferred to Phase 3.

## Data model enrichment (back-compatible)

Light backends leave the new fields empty, so nothing regresses.

- `Symbol.qualified_name: str = ""`.
- New **types** table + `TypeRecord(qualified_name, kind, bases, interfaces, file, line)`
  with `set_types` / `load_types`.
- `CallEdge`: `edge_kind` (`direct` default) + qualified caller/callee ends.
- `resolve_calls` prefers qualified matching when present (exact), falls back to name.

Consumers (Phase 2): qualified-name call resolution, a **Type Hierarchy** docs page, and
`edge_kind` annotations in the call map + API reference.

## Security & sandboxing

Compiling executes **untrusted build logic** (CMake, MSBuild targets, restore scripts).
The container is the safety boundary:

- non-root; **read-only source mount** + ephemeral tmpfs build dir; dropped capabilities;
  seccomp; no docker socket; CPU / memory / **wall-clock** limits.
- **`--network none`** — offline-only (see locked decision #2). Builds that genuinely
  need restore fail with a clear, actionable message rather than reaching the network.
- FIPS posture preserved (UBI9 base). Every deep run is **audit-logged** (CMMC) with
  toolchain provenance (versions, flags, `compile_commands` hash).

## Orchestration & UX

- **Config:** `affordances.analysis_backend: light | heavy | auto`,
  `affordances.analyzer_runtime: docker | podman`, `affordances.analyzer_image_dotnet`.
- **CLI:** `secagent analyze deep <repo>` — selects the language(s), `docker run`s the
  optional image (`--network none --read-only`), captures the contract JSON, validates
  it, and ingests/enriches the store. `secagent analyze deep --ingest <json>` ingests a
  pre-produced report (mirrors `analyze ingest`; needs no container — the testable seam).
- Image/runtime absent → fall back to light + tell the user how to build it
  (`make analyzer-dotnet`).
- **Images** (`secagent-analyzer-dotnet`, later `secagent-analyzer-cpp`) are built by
  dedicated Makefile targets and kept **out of the default image** (size).

## Caching

Cache the normalized report keyed by (git tree / content hash, toolchain version, build
flags); reuse `compile_commands.json`; `ccache` / incremental MSBuild inside the
container for fast re-runs. Extends the existing `ContentCache` idea.

## Phased rollout (C# first)

| Phase | Deliverable | Container? |
|-------|-------------|-----------|
| **0** | Contract dataclasses + validation, store enrichment (qualified names, types table, `edge_kind`), `analyze deep --ingest <json>` path, tests | **No** — pure Python, testable with a hand-written report |
| **1** | `secagent-roslyn` tool + `secagent-analyzer-dotnet` UBI9 image + `make analyzer-dotnet` + `heavy.py` runner; `analyze deep` runs it for C# (offline) | Yes (.NET SDK) |
| **2** | Consumers: qualified-name call resolution, **Type Hierarchy** docs page, `edge_kind` in call map + API reference | No |
| **3** | C/C++ clang-build image reusing the same contract | Yes (clang/LLVM) |
| **4** | Build-artifact caching (git tree + toolchain key), provenance/audit, incremental | No |

Phase 0 needs no container: it lands the contract + ingest + store schema behind
`analyze deep --ingest`, so the whole data path (and the Phase 2 docs/call-map
improvements) can ship and be tested before the heavy image exists.

## Deferred: static analysis

Optional static-analysis capabilities (clang-tidy / clang static analyzer / cppcheck /
CodeChecker for C/C++, Roslyn analyzers for C#) are intentionally **out of scope for the
heavy pipeline** and belong in the existing analysis use cases:

- **UC3** (`secagent analyze`, IKOS today) — fold additional C/C++ static analyzers and
  their findings here; the heavy C/C++ image already builds the project, so it can host
  these analyzers and emit findings via the existing `analyze ingest` report path.
- **UC4** (`secagent scan`) — Roslyn analyzers / .NET source analyzers for the rule-based
  scan.

Tracked as a follow-up; the contract's optional `diagnostics` field is reserved so heavy
backends *can* carry findings into UC3/UC4 once that work is scheduled.
