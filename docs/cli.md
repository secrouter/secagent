# CLI reference

All commands accept `--config / -c <path>` to load a YAML config. Run `secagent --help`
or `secagent <command> --help` for the authoritative, version-specific listing.

## Top level

```text
secagent version                 # print the version
secagent doctor [--probe]        # FIPS + dependency self-checks (--probe hits the endpoint)
secagent config                  # print effective config (secrets redacted)
secagent index <repo> [--no-llm] [--refresh] [--refresh-summaries] [-v]
                               # build/update the affordance store
secagent purge <repo> [--yes]    # securely delete a repo's affordance store (CMMC-2)
```

`-v/--verbose` streams progress to **stderr** — scan/summarize counts, the clang
parse (per-file function tallies), and the map-building phases — so a long run shows
where it is. The JSON result still goes to stdout, so `secagent index <repo> -v >out.json`
keeps progress on the terminal and the report in the file. Also on `docs build`.

`--refresh-summaries` regenerates the LLM summaries/descriptions even when cached. The
cache key already includes the **model name**, so simply changing `llm.model` between
runs regenerates the summaries with the new model (and keeps each model's output cached
separately); this flag is for re-evaluating the *same* model. The clang AST parse is
cached by file hash, so re-indexing while you swap models does not re-parse C/C++.

## `secagent affordance`

The bash-callable query surface pi drives. Each prints text or JSON to stdout. The
store is auto-built (heuristic-only) if missing.

```text
secagent affordance structure <repo>
secagent affordance io <repo>
secagent affordance components <repo>
secagent affordance summary <repo> <path>
secagent affordance functions <repo> <path>
secagent affordance calls <repo> [path]
secagent affordance callers <repo> <symbol>      # who calls a function — check before changing it
secagent affordance types <repo> [name]          # declared types + inheritance (heavy backends)
secagent affordance cache <repo> [--prune DAYS | --clear]   # LLM cache size / reclaim space
secagent affordance summaries <repo> [-o FILE] [--raw]   # per-model manifest (purposes + fn docs)
secagent affordance plan <repo>                  # UC0: components binned by language + tools to run
secagent affordance find-symbol <repo> <name>
secagent affordance search <repo> <query>
secagent affordance context <repo> <query>
secagent affordance slice <repo> <path> [--start N] [--end M]
```

`summaries` dumps a JSON manifest of every generated file purpose and function
description, tagged with the model that produced them — diff two models' manifests to
evaluate quality. `--raw` instead dumps the literal content-cache entries (with per-kind
counts); those are hash-keyed, so not attributable to a file/function.

## `secagent docs`

```text
secagent docs build <repo> [-o OUT] [--no-build] [--no-llm] [--refresh-summaries] [-v]
```

`--no-build` writes the Sphinx sources + `.drawio` files but skips `sphinx-build`.
`--no-llm` uses heuristic prose (no endpoint needed). `--refresh-summaries` forces
regeneration of the LLM summaries. `-v/--verbose` streams the build phases
(index → diagrams → function descriptions → render → Sphinx) to stderr. Each build also
writes `summaries.json` and `summaries.md` (the per-model manifest) into the output dir.

## `secagent testgen`

```text
secagent testgen <repo> [-o OUT] [--no-unit] [--no-functional]
```

UC5: generate unit + functional component I/O tests into a separate folder (default
`<repo>/secagent-tests/`). Run UC1 (`secagent docs build`) first for best results. See
{doc}`use-cases`.

## `secagent scan`

```text
secagent scan <repo> [-o OUT] [--rules <profile.yaml>] [--max-files N] [-v]
```

UC4: LLM rule-based memory/stability scan against a configurable rule set
(`config/rules/*.yaml`, default `embedded-cpp.yaml`). Each profile declares the languages
it applies to, so `embedded-cpp.yaml` scans C/C++ and `rust-safety.yaml` scans Rust; a
profile never applies its rules to a language it was not written for. See {doc}`use-cases`.

`-v` prints per-file progress on stderr. A scan costs one model call per file — minutes
per file on a local model — so without it there is no output at all until the run ends.

The report distinguishes **"no findings"** from **"could not analyse"**. Files whose model
call failed are listed under `failures` in `scan.json`, counted in the summary, and
flagged with an INCOMPLETE SCAN banner in the Markdown: a clean-looking report is only an
all-clear when `analysis_complete` is true.

## `secagent analyze`

```text
secagent analyze run <repo> <target> [-o OUT] [--no-llm]      # run IKOS on a .c/.cpp/.bc
secagent analyze ingest <repo> <ikos-report.json> [-o OUT] [--no-llm]  # ingest a report
secagent analyze deep <repo> [--ingest <report.json>]         # heavy (compiled) semantic analysis
```

UC3: C/C++ static analysis via IKOS. `ingest` needs no IKOS binary. `--no-llm` skips
triage. See {doc}`use-cases`.

`analyze deep` is the **heavy analysis** path: for C# it runs the optional Roslyn
analyzer container (`make analyzer-dotnet`) over the project *offline* and enriches the
store with fully-resolved qualified symbols, the type/inheritance graph, and semantic
call edges. `--ingest <report.json>` ingests a pre-produced `secagent-analysis/v1` report
instead (no container needed). The light (libclang / tree-sitter) path stays the default
for `index`; see the design at `docs/design/heavy-analysis-pipeline.md`.

## `secagent review`

```text
secagent review mr <project> <mr_iid> [--repo PATH] [--dry-run]
secagent review serve [--host 0.0.0.0] [--port 8080] [--tls-cert C --tls-key K --tls-ca CA]
secagent review poll <project> [--repo PATH] [--once]
```

`--dry-run` prints the review instead of posting. `--repo` supplies a local checkout
for affordance-aware reviews. `serve` runs the webhook receiver; `poll` is the
webhook-free fallback (loops every `gitlab.poll_interval_s`, or one pass with
`--once`). See {doc}`gitlab-watch` for the full loop + GitLab setup.

## `secagent audit`

```text
secagent audit verify [path]      # verify the audit log's SHA-256 hash chain
```

Exits non-zero if the chain is broken (edited/inserted/deleted records). Defaults to
the configured `audit.path`. See {doc}`configuration` and {doc}`cmmc`.

## `secagent mcp`

```text
secagent mcp affordances <repo>   # serve affordance tools over MCP (stdio)
secagent mcp gitlab               # serve the GitLab harness over MCP (stdio)
```
```{note}
pi has no built-in MCP; the Skill/extension is the primary pi integration. These MCP
servers are provided for other MCP clients.
```
