# LeanCTX — context compression

secagent integrates [LeanCTX](https://github.com/yvgude/lean-ctx) (Apache-2.0), a local
context-compression layer that shrinks what the agent reads and what gets sent to the model —
typically far fewer tokens per turn, at no change to the answers you get back. It is **on by
default** and **locked down** for the CMMC / air-gapped posture.

secagent already ships an affordance engine (structure/signatures/slice tools) so local models
stay within their window; LeanCTX complements it with a general compression layer that also covers
shell/tool output and the model wire.

## Two layers

| Layer | What compresses | Provided by | Runs |
|---|---|---|---|
| **pi tools + wire compressor** | the agent's file reads, shell/tool output, and its model requests before SecRouter | the `lean-ctx` binary + `pi-lean-ctx` extension | inside pi |
| **secagent own-call compression** | secagent's *own* SecRouter requests — the Mattermost chat bridge (UC101) and MR review (UC100) | the `lean-ctx-client` SDK → the local daemon | inside secagent |

The tuned scan / testgen / docs prompts are deliberately **not** compressed — their prompts are
measured and must not change.

## Security posture (read this)

LeanCTX sees prompt content, so it sits **inside the accreditation boundary** and is locked down by
default. `secagent doctor` verifies these and fails on the ones marked ⛔:

- ⛔ **Loopback only** — the daemon binds `127.0.0.1` (`leanctx.endpoint`). A routable endpoint is a
  doctor error.
- ⛔ **No telemetry** — off by default upstream; the suite keeps it off (`leanctx.telemetry=false`).
- **No phone-home** — the update-check is disabled (`LEAN_CTX_NO_UPDATE_CHECK=1`), so it never
  reaches the network.
- **Hardened** — `lean-ctx harden` (`LEAN_CTX_HARDEN=1`) tightens its MCP config + shell surface.
- **No CUI at rest** — the persistent context/knowledge store is **off** by default
  (`leanctx.persist_context=false`); nothing writes prompt-derived context to disk. Turning it on
  (for LeanCTX's memory features) keeps the store under `leanctx.state_dir`, owner-only — **treat it
  as CUI**: mark it, protect it, and include it in your media-protection scope.
- **Prompt-cache safe** — the wire compressor uses `history_mode = "cache-aware"`, keeping the
  request prefix byte-stable so SecRouter/SecLLM prompt-caching keeps hitting.

The env lockdown is applied to every LeanCTX process regardless of any hand-edited `config.toml`, so
it can't be silently dropped.

## Configuration (`leanctx`)

Every option, with its locked-down default — see `secagent.config.LeanCtxConfig`:

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Master switch. `false` = no LeanCTX install/config/routing at all. |
| `endpoint` | `http://127.0.0.1:4444` | Local daemon (loopback only). |
| `pi_mode` | `additive` | `additive` (pi builtins + `ctx_*`) or `replace` (only `ctx_*`). |
| `pi_enable_mcp` | `false` | Register LeanCTX's advanced MCP tools with pi. |
| `compress_own_calls` | `true` | Compress secagent's own chat/review calls via the SDK. |
| `persist_context` | `false` | Persistent memory store (**CUI at rest** when on). |
| `state_dir` | `~/.secagent/leanctx` | State location (owner-only). |
| `no_update_check` | `true` | Disable the update phone-home. |
| `harden` | `true` | Apply `lean-ctx harden`. |
| `telemetry` | `false` | Never enable telemetry. |
| `proxy_history_mode` | `cache-aware` | Keep the SecRouter prompt cache hitting. |
| `version` / `client_version` | pinned | Supply-chain pins (`lean-ctx` / `lean-ctx-client`). |

Turn it off entirely:

```yaml
# ~/.secagent/config.yaml
leanctx:
  enabled: false
```

or `SECAGENT_LEANCTX__ENABLED=false`, or `secagent init --no-leanctx` for one run.

## Install & setup

`install.sh` installs the pinned binary + pi extension (`lean-ctx-bin` / `pi-lean-ctx`) and the SDK
(`lean-ctx-client`) — all best-effort, so an air-gapped or npm/PyPI-less host still installs fine
(compression simply passes through what's missing). Manually:

```bash
npm install -g lean-ctx-bin@3.9.17 pi-lean-ctx@3.9.17   # binary + pi extension
pip install 'secagent[leanctx]'                          # the SDK (own-call compression)
```

`secagent init` writes the locked-down `~/.config/lean-ctx/config.toml` (`0600`) and best-effort
runs `lean-ctx init --agent pi` + `lean-ctx harden` with the lockdown env applied. A missing binary
never fails init — it's reported, the config is still written, and `secagent doctor` flags it.

## Verify

```bash
secagent leanctx     # config + lockdown + what's installed (read-only)
secagent doctor      # runs the `leanctx` health check (loopback/telemetry are hard failures)
```

## Graceful by contract

LeanCTX is **optional and never fatal**. If the binary/SDK isn't installed or the daemon is down:
the pi tools fall back to pi's builtins, and secagent's own-call compression passes the messages
through unchanged. A compression outage never drops or corrupts a governed request.

```{note}
A few LeanCTX runtime behaviours (the exact SDK compress call shape, and how `cache-aware`
interacts with your SecRouter/SecLLM prompt cache) are best confirmed against the running daemon on
the deployment host. The lockdown, config, and graceful fall-through are enforced in secagent
regardless; `secagent doctor` reports what's actually present.
```
