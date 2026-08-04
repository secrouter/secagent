# Running secagent under pi (pi.dev)

**pi is the agent runtime; secagent is the context-frugal toolset it drives.** pi owns
the loop, tools (read/write/edit/bash), sessions, and provider selection. secagent adds
the affordance engine (so local models stay within their window), Sphinx+Draw.io docs
generation, and GitLab MR review.

## 1. Install

```bash
# pi (TypeScript/Node coding agent)
npm install -g @earendil-works/pi-coding-agent      # or: curl -fsSL https://pi.dev/install | sh

# secagent (Python toolset)
pip install "secagent[docs,review,tokenizer]"
```

## 2. Point pi at a local Gemma model

Copy `models.example.json` to `~/.pi/agent/models.json` and set `baseUrl` to your
OpenAI-compatible endpoint:

```bash
# llama.cpp:  llama-server -m gemma-3-12b-it-Q4_K_M.gguf --host 0.0.0.0 --port 8000
# vLLM:       vllm serve google/gemma-3-12b-it --port 8000
pi --provider local-gemma --model gemma-3-12b-it
```

## 3. Load the secagent extension (optional but recommended)

The extension registers secagent's affordance tools so the model can call
`secagent_structure`, `secagent_io_map`, `secagent_search`, `secagent_file_summary`,
`secagent_find_symbol`, `secagent_functions`, `secagent_calls`, `secagent_callers`,
`secagent_types`, `secagent_context`, `secagent_read_slice`, `secagent_plan`, plus
`/secagent-analyze-all`, `/secagent-plan`, `/secagent-docs`, and `/secagent-review`.

```bash
# load per-session
pi --extension ./pi/extensions/secagent.ts
# or install it into your pi config so it loads automatically (see pi docs)
```

Set `SECAGENT_REPO` to the repository you're working on (defaults to pi's cwd).

If you'd rather not use the extension, the **Skill** in `skills/secagent/SKILL.md` lets
pi use the same capabilities through its bash tool + this README — no TypeScript
needed. (pi has no built-in MCP; Skills/extensions are the integration path.)

## 3b. Point pi at SecRouter — per-user login

`pi/extensions/secrouter-auth.ts` registers a `secrouter` provider whose `/login`
drives SecSSO's OIDC **device-authorization** flow, so each developer authenticates
as themselves rather than sharing a static key. pi stores + auto-refreshes the
resulting tokens in `~/.pi/agent/auth.json` (0600); this extension only implements
the SecSSO REST calls.

```bash
export SECROUTER_SSO_DEVICE_URL=https://secsso.<domain>/realms/secrouter/protocol/openid-connect/auth/device
export SECROUTER_SSO_TOKEN_URL=https://secsso.<domain>/realms/secrouter/protocol/openid-connect/token
pi --extension ./pi/extensions/secrouter-auth.ts
```
```
/login secrouter    # device code shown in-terminal; approve it in a browser
/model               # pick a secrouter/<id> model
```

Copy `pi/models.secrouter.example.json` to `~/.pi/agent/models.json` (or merge it
alongside another provider's entry) to set the deployment's real `baseUrl` — see
`https://secrouter.<domain>:47002/v1` — and the model catalog; the extension supplies
only the OAuth flow, not the models list (same split `docs/models.md` documents for
Ollama/vLLM). This is the per-user counterpart to `secagent token`
({doc}`../docs/configuration` "Inference at SecRouter"), which is a *service* identity
(OIDC client_credentials) for automated/headless calls — same SecSSO IdP, two
different grants for two different kinds of caller.

```{note}
Verified against `@earendil-works/pi-coding-agent@0.83.0` / `@earendil-works/pi-ai@0.83.0`
(`pi.registerProvider`'s `oauth` field and the `OAuthLoginCallbacks`/`OAuthCredentials`
shapes), since pi itself was not installed in the environment this was written in — see
the file's own header comment for exactly what was cross-checked. Re-verify against
whatever pi version is actually deployed, the same caveat `pi/extensions/secagent.ts`
already carries for its own (unrelated) API surface.
```

## 4. The use cases

**Full analysis (UC0).** Drop pi into an unfamiliar project and have it bin the
components by language, run the right secagent tools on each, and synthesize a summary:

```
> /secagent-analyze-all ./out
```

It indexes, prints the per-language plan, and builds the docs + summaries; then follow
the `skills/secagent-analysis/SKILL.md` playbook to run the per-language tools and — by
default — write `ANALYSIS.md`, a codebase summary that **embeds secagent's Draw.io diagrams**
(the architecture + data-flow graphs the docs build produces deterministically from the IO
map — pi doesn't hand-author diagrams). Add `--no-summary` to stop after the per-language
tools.

**Deep-dive docs (UC1).** Let pi loop over the repo using the affordance tools, then
build the site:

```
> Explore this service with the secagent_* tools and write architecture notes,
> then run /secagent-docs ./site
```

**GitLab MR review (UC100).** Interactive:

```
> /secagent-review mygroup/myproject 42 --dry-run
```

Autonomous (new MRs + @-mentions) runs as the Python webhook service, which reuses the
same affordance store:

```bash
secagent review serve --port 8080
```

## FIPS note

pi runs on Node; run it on a Node built against FIPS OpenSSL (`node --enable-fips`) on
a FIPS host. All of secagent's hashing/TLS/secret handling stays in the Python layer
(SHA-256 only, system OpenSSL). See `../docs/FIPS.md`.
