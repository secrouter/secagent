# Skill: secagent affordances

Use the `secagent` CLI to understand a codebase **without reading whole files**. secagent
pre-indexes the repo into compact affordances (structure, file summaries, a
service/component IO map, a symbol index) and serves them as small, budget-bounded
chunks. Prefer these over `cat`/reading large files — they keep context small, which
matters for local models.

Run once per repo (fast, incremental, cached):

```bash
secagent index <repo>            # or it auto-builds on first affordance query
```

Then query (all print to stdout; JSON where noted):

| Command | Use it to… |
|---------|------------|
| `secagent affordance structure <repo>` | get the high-level map (components, languages, entrypoints) |
| `secagent affordance io <repo>` | see how components connect (imports, HTTP endpoints, outbound calls, datastores) |
| `secagent affordance search <repo> "<query>"` | find the most relevant files (JSON: path + purpose) |
| `secagent affordance summary <repo> <path>` | get one file's purpose, key symbols, IO signals (JSON) |
| `secagent affordance find-symbol <repo> <name>` | locate a function/class (JSON: file + line + signature) |
| `secagent affordance context <repo> "<query>"` | get a ready-to-use, budget-bounded context block |
| `secagent affordance slice <repo> <path> --start N --end M` | read only the lines you need |

## Recommended loop for a deep dive

1. `secagent affordance structure <repo>` — orient.
2. `secagent affordance io <repo>` — understand the wiring.
3. For each area of interest: `secagent affordance search`, then `summary`/`find-symbol`,
   and only `slice` the exact lines you must see.
4. Write findings/docs. To produce a full Sphinx site with rendered Draw.io diagrams:
   `secagent docs build <repo> -o <out>`.

## Reviewing a GitLab merge request

```bash
secagent review mr <project> <mr_iid> --repo <repo> [--dry-run]
```

Behaviour (alignment, verbosity, focus) is set by the persona file in
`config/alignment/*.yaml` — edit it, no restart needed.

To watch a whole project continuously, run the webhook receiver
(`secagent review serve`) or, on air-gapped instances, poll
(`secagent review poll <project> [--once]`). See `docs/gitlab-watch.md` for the GitLab
bot/token/webhook setup.

## C/C++ static analysis (IKOS)

```bash
secagent analyze run <repo> <file.c|.bc> -o <out>        # invoke IKOS, then report
secagent analyze ingest <repo> <ikos-report.json> -o <out>   # ingest an existing report
```

Produces `analysis.md` + `analysis.json`: IKOS findings (buffer overflow, null deref,
integer overflow, division by zero, …) enriched with the owning component, the file's
purpose, and an optional one-line triage. `ingest` needs no IKOS binary.

## Memory/stability scan (configurable rules)

```bash
secagent scan <repo> -o <out> [--rules config/rules/<profile>.yaml]
```

The local model reviews C/C++ against a configurable, heuristic rule set
(`config/rules/embedded-cpp.yaml` by default — Power of Ten / MISRA / CERT / BARR-C).
Edit the YAML to add/remove rules. Produces `scan.md` + `scan.json` with rule id,
severity, `file:line`, and the owning component.

## Automatic test generation

```bash
secagent docs build <repo>     # UC1 first (recommended)
secagent testgen <repo> -o <out>   # default: <repo>/secagent-tests/
```

Drafts unit tests (per file) and functional component I/O tests (from the IO map) into
a separate top-level folder (`unit/`, `functional/`, `manifest.json`, `README.md`).
Generated tests are drafts — review before adding to CI.
