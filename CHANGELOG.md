# Changelog

All notable changes to secagent are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versions follow the tags in
`git log`. Merge-commit noise is collapsed into the change it merged.

## [Unreleased]

Everything below has landed on `main` since the `v0.3.0` tag but hasn't been cut into
a release yet.

- `secagent evidence [--out DIR]` — writes a CMMC self-assessment evidence bundle
  (product/version, a sanitized config posture, the audit-chain verification result,
  recent audit records, and a control self-assessment). See
  [docs/cli.md](docs/cli.md).
- `docs/cmmc.md`'s control tables now cite bare NIST SP 800-171 **Family + ID**
  (e.g. `AU` / `3.3.8`) instead of CMMC assessment-guide practice IDs
  (`AU.L2-3.3.8`), matching the citation style `secagent evidence` emits.
- Sphinx docs theme: brand colors (light/dark `color-brand-primary` /
  `color-brand-content`) and a light/dark logo pair, matching the rest of the suite.
- **Knowledge graph**: push-retrieval code memory for pi — a symbol/import/call-edge
  graph built per language, with case-sensitive identity, file-qualified symbols to
  resolve name collisions, cross-file call disambiguation + ranking, and CommonJS
  `exports.x = function` support. Later hardened with **semantic (type-resolved)**
  call extraction per language and an `edge_kind` distinguishing it from the fast
  syntactic pass — Python via `jedi` (`python-semantic` extra), Go via a type-checker
  frontend. See [docs/knowledge-graph.md](docs/knowledge-graph.md).
- `analysis_run` pi extension: run analyzer containers on Docker **or** the k8s pool,
  not just Docker.
- pi-guard + loop-breaker + LeanCTX `allow_paths`: headless-agent failures are now
  visible and survivable instead of hanging or silently looping.
- LeanCTX now attaches to pi at **launch time** instead of wrapping the whole host
  shell (and, by extension, the operator's own coding-agent CLI) — see
  [docs/leanctx.md](docs/leanctx.md).
- Removed the SecAgent→Mattermost chat bridge; superseded by native SecChat.
- CI installs the `python-semantic` extra so the knowledge graph's `jedi`-backed deep
  path is actually exercised, alongside the `clang`/`csharp`/`rust` backends already
  installed in the verify job.

## [0.3.0] - 2026-08-06

- **LeanCTX** context-compression integration: config + a locked-down-by-default
  core that compresses secagent's own SecRouter calls, pinned install wired into
  `secagent init`, and a `secagent leanctx` status subcommand + `doctor` check. See
  [docs/leanctx.md](docs/leanctx.md).
- Fresh-Mac install docs: Homebrew bootstrap, the macOS system-Python-3.9 caveat, and
  the `~/.local/bin` PATH fix after `uv tool install`.
- CI installs the language-analysis backends (`clang`/`csharp`/`rust`) in the verify
  job so those code paths are actually exercised.

## [0.2.1] - 2026-08-04

- `--range`: a fixed-commit-range scope option alongside `--base`/`--since`/`--staged`/
  `--working-tree`/`--path` on the git-aware commands.

## [0.2.0] - 2026-08-04

- **Git-scoped runs**: `secagent docs build` and `secagent testgen` refresh only the
  files changed since your branch's base by default, instead of the whole repository;
  `secagent scan` and a new `secagent review local` do the same for surgical,
  no-GitLab-needed scans/reviews over a local delta. See
  [docs/git-scope.md](docs/git-scope.md).
- **Developer onboarding**: `secagent init --domain` + `secagent login`/`logout`/
  `token --user` — one-command setup against an existing SecRouter deployment, with a
  per-user OIDC device-authorization identity distinct from the service identity. See
  [docs/installation.md](docs/installation.md).
- SecAgent chat-ops: a Mattermost bot handler + OIDC auth helpers, and an audit event
  for chat interactions plus SecRouter inference config. (The chat-ops bridge was
  later removed in favor of native SecChat — see Unreleased above.)

## [0.1.0] - 2026-08-03

- Initial release: the **affordance engine** (structure map, per-file summaries, IO
  map, symbol index, inter-file call map), the pi integration, and the first two use
  cases — the Sphinx + Draw.io **docs** deep-dive (UC1) and the GitLab **MR review**
  bot (UC100) — plus the FIPS-compatibility posture and the CMMC Level 2 control
  mapping ([docs/cmmc.md](docs/cmmc.md)) that later releases build on.
