# Configuration

Settings are layered, lowest precedence first:

1. Built-in defaults.
2. `~/.secagent/config.yaml`, if present — a per-user layer written by `secagent
   init` (see {doc}`installation`'s developer quickstart), so a developer's own
   SecRouter/SecSSO wiring applies to every `secagent` invocation with nothing to
   `export` by hand. Clearly opt-in: absent (the state before running `secagent
   init`, or on a service/CI host that never runs it) means no change in behavior at
   all versus not having this layer.
3. A YAML file (via `--config`, or the `SECAGENT_CONFIG` env var).
4. `SECAGENT_*` environment variables (nested with `__`).

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

### `secsso`

OIDC settings for secagent's TWO SecSSO identities — see `src/secagent/secsso.py`.

**Service identity** (`token_url`/`client_id`/`username`/`client_secret_env`/
`scope`/`token_cache_path`): `secagent token`, an OAuth2 `client_credentials` grant
for headless/automated calls — see "Inference at SecRouter" above.

**Per-user identity** (`device_authorization_url`/`device_client_id`/`device_scope`/
`user_token_cache_path`): `secagent login` / `secagent logout` / `secagent token
--user`, an OIDC device-authorization grant (RFC 8628) for an individual developer's
own interactive use — see {doc}`installation`'s developer quickstart, which is the
normal way these fields get set (`secagent init --domain ...` writes them into
`~/.secagent/config.yaml`, not this file, for a given developer). `token_url` is
shared by both identities — SecSSO serves one token endpoint per instance regardless
of which grant/client is authenticating against it.

```yaml
secsso:
  # -- service identity ("secagent token") --
  token_url: ""                # SecSSO's OIDC token endpoint; empty = `secagent token`/`--user` refuse to run
  client_id: "secagent"
  username: "svc-secagent"     # informational only -- never sent as a grant parameter
  client_secret_env: "SECAGENT_CLIENT_SECRET"   # NAME of the env var holding the secret
  scope: "openid secrouter"
  token_cache_path: "~/.secagent/auth/secsso-token.json"   # 0600; not per-repo
  expiry_buffer_s: 60          # refresh this many seconds before actual expiry; shared by both caches

  # -- per-user identity ("secagent login" / "secagent token --user") --
  device_authorization_url: "" # SecSSO's OIDC device_authorization endpoint; empty = `secagent login` refuses to run
  device_client_id: "secagent-pi"   # PUBLIC client -- no secret (RFC 8628 SS3.1)
  device_scope: "openid profile email secrouter"
  user_token_cache_path: "~/.secagent/auth/user-token.json"   # 0600; a DIFFERENT file/identity than token_cache_path
```

```bash
secagent token                 # prints a fresh (or cached) SERVICE bearer token to stdout
secagent login                 # interactive: device-code sign-in, caches YOUR OWN token
secagent token --user          # prints a fresh (or cached) PER-USER bearer token to stdout
secagent logout                # deletes the cached per-user token
```

Both `token` forms cache the fetched token on disk (`token_cache_path` /
`user_token_cache_path`) so either is cheap to invoke on every request — which is
exactly how pi re-invokes a `models.json` `"!command"` `apiKey` (see `pi/docs/models.md`
"Value Resolution"): once per actual LLM call, not once at startup. Point pi and
secagent's own `llm.api_key` at whichever identity fits the caller:

```yaml
llm:
  api_key: "!secagent token"          # service identity (headless/automated)
  # or:
  api_key: "!secagent token --user"   # per-user identity (an individual developer)
```

`llm.api_key` supports the identical literal / `$ENV_VAR` / `"!command"` resolution
syntax pi does (`src/secagent/secretval.py`), resolved fresh on every LLM request —
not cached at client construction — so a refreshing `"!command"` value stays current.
`secagent doctor --probe`'s `llm_endpoint` check resolves it the same way before
probing, rather than sending the literal `"!command"` string as a credential.

### `mattermost`

UC101: `secagent chat serve`, secagent's own transport (not the `pi-mattermost`
plugin) for Mattermost slash commands and outgoing webhooks. Same hardening posture
as `gitlab`: `chat serve` refuses to start without `webhook_secret` (or an explicit
`webhook_allow_unauthenticated` opt-out), and supports the same `--tls-*` / mTLS
options as `review serve`.

```yaml
mattermost:
  url: ""                      # Mattermost server base URL, e.g. https://chat.example.com
  bot_token: ""                # OUTBOUND: posts replies as the bot (prefer SECAGENT_MATTERMOST__BOT_TOKEN)
  team: ""                     # team name/ID the bot operates in
  bot_username: "secagent"     # recognized mention prefix; ignores the bot's own posts
  verify_tls: true
  webhook_secret: ""           # INBOUND: the `token` Mattermost sends per slash command/webhook
  webhook_allowed_ips: []      # CMMC-4: source-IP allow-list ([] = any); pair with mTLS
```

```bash
secagent chat serve --port 8070
```

`bot_token` and `webhook_secret` are two DIFFERENT secrets: `bot_token` authenticates
secagent's own outbound REST calls to Mattermost (posting the reply); `webhook_secret`
authenticates Mattermost's inbound deliveries to secagent (the shared `token` field
Mattermost sends with every slash-command/outgoing-webhook POST). Every chat
interaction is recorded via `AuditLogger.record_chat` with the invoking Mattermost
user as `end_user` — distinct from the bot's own service `principal` — see `audit`
below.

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
