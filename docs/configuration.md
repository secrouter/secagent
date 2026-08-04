# Configuration

Settings are layered, lowest precedence first:

1. Built-in defaults.
2. A YAML file (via `--config`, or the `SECAGENT_CONFIG` env var).
3. `SECAGENT_*` environment variables (nested with `__`).

Start from `config/secagent.example.yaml`. Inspect the effective config (secrets
redacted) with:

```bash
secagent config
```

## Key sections

### `llm`

Connection + budgeting for the external OpenAI-compatible endpoint.

```yaml
llm:
  base_url: "http://gemma-host:8000/v1"   # llama.cpp or vLLM
  api_key: "not-needed"
  model: "gemma-3-12b-it"
  context_window: 131072      # your model's real window (default suits Gemma 3/4)
  context_budget_ratio: 0.6   # fraction of the window for working context
  max_output_tokens: 16384    # reasoning models need >12k before emitting content
  temperature: 0.2
  native_tool_calls: true     # set false for llama.cpp builds without tool-calling
```

```{important}
Set `context_window` to your model's real window: Gemma 2 = 8192, Gemma 3 1B = 32768,
Gemma 3 4B/12B/27B = 131072, Gemma 4 up to 262144. The default suits a modern local
deployment; lower it for a small model. `secagent doctor --probe` reads the window your
server actually serves and flags a mismatch either way.

This sizes more than the prompt: the retry that rescues an empty reasoning-model response
is bounded by it, so setting it too low disables that recovery entirely.
```

Override per-process with env vars, e.g.:

```bash
export SECAGENT_LLM__BASE_URL="http://gemma-host:8000/v1"
export SECAGENT_LLM__CONTEXT_WINDOW=131072
```

#### Inference at SecRouter

`llm.base_url` is endpoint-agnostic — anything that speaks the OpenAI `/v1/chat/completions`
shape works, including **SecRouter**, the suite's LLM gateway. Point secagent at it the
same way you would any other endpoint, no code changes:

```bash
export SECAGENT_LLM__BASE_URL="https://secrouter.<domain>:47002/v1"
export SECAGENT_LLM__MODEL="<model name SecRouter exposes for this token>"
export SECAGENT_LLM__API_KEY="<bearer token SecRouter issued>"
```

or in YAML:

```yaml
llm:
  base_url: "https://secrouter.<domain>:47002/v1"
  api_key: "<bearer token SecRouter issued>"
  model: "<model name SecRouter exposes for this token>"
```

`api_key` is sent as `Authorization: Bearer <api_key>` (`llm/client.py`) — that header is
how SecRouter authenticates the request — and, like every other secret in this config, it
is never logged and is redacted (`***`) by `secagent config`. TLS is verified against the
system (FIPS) trust store the same as any other `https://` endpoint; pair this with
`network.require_tls` / `network.allowed_hosts` below if you want secagent to refuse to
fall back to an unlisted endpoint. `secagent doctor --probe` works unchanged — it queries
whatever `base_url` currently points at, SecRouter included.

### `affordances`

```yaml
affordances:
  store_dir: ".secagent"        # where the content-addressed store lives
  llm_summaries: true         # heuristic + LLM file purposes (false = heuristic only)
  refresh_summaries: false    # force-regenerate LLM summaries (ignore cache on read)
  max_file_bytes: 200000
  ignore_vcs: true            # skip the project's own VCS metadata in scans
  clang_compile_db: ""        # path to compile_commands.json (empty = autodiscover)
  clang_extra_includes: []    # extra -I dirs for best-effort clang parsing
  max_function_docs: 120      # per-function LLM descriptions per docs build (cached); -1 = all
  analysis_backend: light     # light | heavy | auto — `analyze deep` backend selection
  analyzer_runtime: docker    # container runtime for the heavy backends (docker | podman)
  analyzer_image_dotnet: secagent-analyzer-dotnet:latest   # C# Roslyn analyzer image
  analyzer_image_rust: secagent-analyzer-rust:latest       # Rust rust-analyzer image
```

`ignore_vcs` (default `true`) excludes the project's own version-control
metadata/tooling from the architecture and documentation scans: the `.git` directory
at any depth (so submodules and monorepos are covered), submodule `.git` pointer
files, Git dotfiles (`.gitignore`, `.gitmodules`, `.gitattributes`, …), other VCS
dirs (`.svn`, `.hg`, `.bzr`), and repository-host platform config (`.github`,
`.gitlab` — CI workflows, issue/PR templates, CODEOWNERS). It matches by **reserved
names only**, so source files that *use or implement* Git as a feature — a `vcs/git`
module, a GitLab client, a `git-workflow.md` doc — are still indexed. Set it to
`false` to index the VCS material too.

**C/C++ analysis (clang).** When the `clang` extra is installed (`pip install
'secagent[clang]'`, which bundles libclang), secagent extracts accurate **functions** and
an inter-file **call map** from the C/C++ AST, surfaced by `secagent affordance functions
<file>` / `secagent affordance calls` and in the generated docs (a *Call Map* page plus
per-function descriptions). A `compile_commands.json` gives the best results — point
`clang_compile_db` at one, or let the agent generate it (`cmake
-DCMAKE_EXPORT_COMPILE_COMMANDS=ON`, `bear -- make`). Without one, secagent parses
best-effort with project headers auto-discovered from the repo root (add SDK/system
dirs via `clang_extra_includes`). If libclang is absent, secagent falls back to regex
symbols / the LLM.

**C# analysis.** With the `csharp` extra (`pip install 'secagent[csharp]'`, tree-sitter)
secagent extracts C# functions + a call map the same way — no .NET SDK needed. For
fully-resolved, *semantic* C# analysis (qualified call graph + type/inheritance), the
**heavy** backend runs the optional Roslyn analyzer container: build it with `make
analyzer-dotnet`, then `secagent analyze deep <repo>`. `analysis_backend` /
`analyzer_runtime` / `analyzer_image_dotnet` / `analyzer_image_rust` select them; each
runs offline and falls back
to the light path when the image is absent. See {doc}`design/heavy-analysis-pipeline`.

**Model evaluation.** The LLM summary/description cache key includes the model name, so
changing `llm.model` regenerates summaries with the new model (each model's output cached
separately). `refresh_summaries: true` (or `--refresh-summaries`) forces regeneration for
the *same* model; `max_function_docs: -1` describes every function (the default 120 covers
only a fraction of a large codebase). `secagent affordance summaries <repo>` dumps the
per-model manifest. See {doc}`running-on-a-project`.

### `scan`

```yaml
scan:
  rules_profile: "config/rules/embedded-cpp.yaml"  # which rule set (declares its languages)
  max_files: 0                 # 0 = the whole project; set N for a quick bounded pass
  max_file_bytes: 40000        # per-file source sent to the model
  max_tokens: 0                # output budget per file; 0 = follow llm.max_output_tokens
```

`max_tokens` matters on a reasoning model: it spends part of the budget before emitting
any content, so too small a value yields an empty response, a retry at a larger budget,
and roughly double the wall time. `0` follows `llm.max_output_tokens` (floor 900).

### `gitlab`

```yaml
gitlab:
  url: "https://gitlab.example.com"
  token: ""                   # prefer SECAGENT_GITLAB__TOKEN / a secret mount
  bot_username: "secagent-bot"
  verify_tls: true
  webhook_secret: ""          # prefer SECAGENT_GITLAB__WEBHOOK_SECRET
  poll_interval_s: 0          # >0 enables the polling fallback (air-gapped)
```

### `persona`

Points at the review persona profile (alignment + verbosity). See {doc}`use-cases`.

```yaml
persona:
  profile: "config/alignment/default.yaml"
```

### `fips`

```yaml
fips:
  require_fips: false         # true = expect FIPS; abort startup on a non-FIPS host
  allow_non_fips: false       # escape hatch: run on a non-FIPS host even if require_fips
  forbid_weak_hashes: true
```

FIPS runtime policy:

- `require_fips: false` (default) — secagent runs anywhere; `doctor` reports a non-FIPS
  host as a warning.
- `require_fips: true` — the agent entry points abort on a non-FIPS host
  (`FIPSComplianceError`), and `doctor` fails the `fips_mode` check.
- `require_fips: true` **+ `allow_non_fips: true`** — run on a non-FIPS host anyway
  (handy for developing/testing a FIPS-configured image); the non-FIPS state is still
  surfaced as a warning. Set via `SECAGENT_FIPS__ALLOW_NON_FIPS=true`.

### `audit`

Structured, tamper-evident audit logging (CMMC-1 / NIST 800-171 AU). Disabled by
default; enable for CMMC/CUI operation. Every agent action and MCP tool call is
written as one append-only JSONL record, SHA-256 hash-chained so tampering is
detectable. Verify with `secagent audit verify`.

```yaml
audit:
  enabled: false                       # enable in CMMC/CUI deployments
  path: ".secagent/audit/audit.jsonl"    # use an absolute, protected, SIEM-forwarded path
  principal: ""                        # SERVICE identity per event (falls back to $SECAGENT_PRINCIPAL)
  echo_stderr: false                   # also emit each record to stderr
  capture_content: false               # chat message/reply text: digest-only vs. verbatim
```

```{tip}
Forward the log to your SIEM and restrict its file permissions — secagent makes records
tamper-evident, but storage protection (AU.L2-3.3.8) is the environment's job.
```

**Chat interactions (UC101).** A chat-driven action (`AuditLogger.record_chat`) carries a
second identity, `end_user` — the Mattermost user who triggered it — kept distinct from
`principal`, which stays the service/bot identity; one bot principal would otherwise
collapse every user into a single attribution. Because the message/reply text is
CUI-sensitive, `capture_content` (default `false`) decides how it is recorded:

- `false` (default) — only a SHA-256 digest of the message/reply is recorded
  (`target.message_sha256` / `target.reply_sha256`); the record contains no CUI.
- `true` — the verbatim text is recorded too (`target.message` / `target.reply`), and
  the whole record is tagged `cui: true` so it can be routed, retained, or
  access-controlled as CUI downstream without inspecting `target`.

Set via `SECAGENT_AUDIT__CAPTURE_CONTENT=true`, or per-call for one interaction
regardless of the configured default. The hash chain and `verify_chain` cover chat
records exactly like any other action.

### `network`

Egress controls (CMMC-3 / NIST 800-171 AC.3.1.3, SC.3.13.6/.8). Disabled by default.

```yaml
network:
  require_tls: false        # refuse non-HTTPS llm/gitlab endpoints (loopback exempt)
  allowed_hosts: []         # egress allow-list of hostnames ([] = no allow-list)
```

When enforced, the agent entry points validate `llm.base_url` and `gitlab.url` before
any outbound call and raise `NetworkPolicyError` on a violation; `secagent doctor`
reports the policy. Loopback endpoints (`localhost`/`127.0.0.1`/`::1`) are exempt —
that traffic never leaves the host.

Secrets (`llm.api_key`, `gitlab.token`, `gitlab.webhook_secret`) are never logged and
are redacted by `secagent config`.
