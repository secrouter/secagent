# Developer quickstart (macOS / Linux)

This page is for an individual developer putting secagent on their **own** Mac or
Linux machine, to use against an **existing SecRouter deployment**, authenticated as
**themselves**. One command installs the tools; two more commands finish setup:

```bash
./install.sh                              # 1. install secagent (+ pi, if Node is present)
secagent init --domain <your-suite-domain>  # 2. wire up pi + secagent for that deployment
secagent login                              # 3. authenticate as yourself
```

No root, no `systemd`, nothing written outside your home directory. If you're
deploying secagent as a shared, unattended **service** instead (a bot account, a
GitLab-review webhook, a chat-ops server) see [How this differs from a SecDeploy
install](#how-this-differs-from-a-secdeploy-service-install) below — that path is
different from this one.

## Before you start (macOS)

`install.sh` never installs system packages or runs `sudo`, so on a fresh Mac you set
up a few prerequisites first. The big one: macOS's built-in `/usr/bin/python3` is
**3.9**, which is too old. `install.sh` looks for a newer `python3.11`/`3.12`/`3.13`
and stops if it can't find one — it will **not** install Python for you.

| prerequisite | why | one-time install |
|---|---|---|
| [Homebrew](https://brew.sh) | the simplest way to get the rest | see brew.sh |
| Xcode Command Line Tools | compiler + headers some wheels build against | `xcode-select --install` |
| **Python 3.11+** | the system `python3` (3.9) is too old | `brew install python@3.13` |
| Node.js *(optional)* | only for **pi**, the agent runtime; secagent's own CLI works without it | `brew install node` |

`uv` is installed by `install.sh` itself if you don't already have it — no need to get
it first. On Apple Silicon everything is native `arm64`; no Rosetta.

From a bare machine, that's just:

```bash
xcode-select --install            # skip if you've compiled anything before
brew install python@3.13 node     # 'node' is optional — only pi needs it
```

Then run the install below.

## 1. Install

```bash
git clone https://github.com/secrouter/secagent
cd secagent
./install.sh
```

(Rather not clone first? `install.sh` never assumes it was, so the one-liner
`curl -fsSL https://raw.githubusercontent.com/secrouter/secagent/main/install.sh | sh`
works too.)

`install.sh` is POSIX `sh`, idempotent, and never needs root. It:

1. Detects your OS/arch (macOS or Linux only).
2. Looks for **Python 3.11+**. If it can't find one, it prints OS-specific install
   guidance and stops — it never tries to install Python for you.
3. Installs [`uv`](https://docs.astral.sh/uv/) if it isn't already on your PATH (via
   uv's official installer, falling back to `pipx`/`pip` if `curl`/`wget` aren't
   available).
4. Installs `secagent` itself with `uv tool install`, from a pinned git ref, so
   `secagent` lands on your PATH.
5. If `npm` is present, installs **pi** (the agent runtime — see below) at a pinned
   version. If `npm` isn't present, it prints guidance and continues anyway: pi is
   optional.

Re-run it any time — every step is safe to repeat.

```{note}
`SECAGENT_REF` and `PI_VERSION` at the top of `install.sh` pin exactly what gets
installed. Override them in your environment (e.g. `SECAGENT_REF=main ./install.sh` for the
latest unreleased build) to install something other than the script's default.
```

```{tip}
**`secagent: command not found` right after installing?** `uv tool install` puts the
`secagent` executable in `~/.local/bin`, which may not be on a fresh Mac's `PATH`. uv
prints the exact fix when that happens: run `uv tool update-shell` (or add
`~/.local/bin` to your `PATH`) and open a new terminal. `install.sh` adds it for the
rest of its own run — so its final `secagent version` check still passes — but your
interactive shell needs it added once.
```

## 2. Point secagent (and pi) at your SecRouter deployment

```bash
secagent init --domain <your-suite-domain>
```

By suite convention, `--domain` alone derives every URL secagent needs:

| peer | derived from `--domain sec.internal` |
|---|---|
| SecRouter (LLM gateway) | `https://secrouter.sec.internal:47002/v1` |
| SecSSO device authorization endpoint | `https://secsso.sec.internal:9000/application/o/device/` |
| SecSSO token endpoint | `https://secsso.sec.internal:9000/application/o/token/` |

If your deployment doesn't follow that convention, override the pieces you need with
`--secrouter-url <full base URL, incl. /v1>` and/or `--secsso-url <base URL, no
path>` — either may be combined with `--domain` to override just one peer.

`secagent init` writes two files, and **only** these two files:

- **`~/.pi/agent/models.json`** — registers a single `secrouter` provider for pi (see
  [pi is optional](#pi-is-optional) below). `--model` (default `balanced`, a
  SecRouter-side routing tier) picks which model id is registered; pass a different
  id/tier if your deployment exposes one you'd rather use.
- **`~/.secagent/config.yaml`** — a per-user config layer, auto-loaded by every later
  `secagent` invocation (see {doc}`configuration`'s precedence list), so secagent's
  *own* use cases (`secagent index`, `secagent scan`, `secagent docs build`, ...) also
  run against SecRouter as you, with nothing to `export` by hand.

Both use `!secagent token --user` as the credential — never a literal secret (see
[Per-user identity](#per-user-identity-secagent-token---user) below). Re-running
`secagent init` is safe: it only ever touches the `secrouter` key of `models.json` and
the `llm`/`secsso` sections of `config.yaml`, so anything else you've added to either
file by hand (another pi provider, another secagent config section) survives. Pass
`--force` to additionally back up the previous version of each file before writing
(`models.json.bak-<timestamp>`, alongside the original).

## 3. Authenticate as yourself

```bash
secagent login
```

This runs an OIDC **device authorization** flow (RFC 8628) against SecSSO — the same
kind of flow `gh auth login`/`az login` use. It prints a verification URL and a short
code:

```text
Sign in to SecSSO to continue:
  https://secsso.sec.internal:9000/if/device/
  (or open https://secsso.sec.internal:9000/if/device/ and enter code: ABCD-1234)
Waiting for approval...
```

Open that URL in **any** browser — this machine, your phone, doesn't matter — sign in
with your own SecSSO identity, and approve. `secagent login` polls until you do, then
caches the result at `~/.secagent/auth/user-token.json` (mode `0600`, directory
`0700`), refreshing it automatically before it expires. Re-run `secagent login` any
time; it always re-authenticates.

```bash
secagent logout   # deletes the cached token
```

## Per-user identity: `secagent token --user`

Everything above exists to make ONE thing true: there is a single, per-user token
source, and both secagent and pi read *the same one*.

```bash
secagent token --user   # prints your cached SecSSO token (refreshing it if needed)
```

This is the exact per-user mirror of the service identity's `secagent token`
(client-credentials — see {doc}`configuration`'s "Inference at SecRouter" section):
same shape, same caching discipline, but backed by *your* OIDC login instead of a
shared client secret. `secagent init` wires both consumers to invoke it as a command,
resolved fresh on every call (never stored):

- pi's `models.json`: `"apiKey": "!secagent token --user"`
- secagent's own config: `SECAGENT_LLM__API_KEY=!secagent token --user` (in
  `~/.secagent/config.yaml`)

If you ever need the raw token for something else (curling SecRouter directly, say),
`secagent token --user` is exactly that: stdout is the token and nothing else on
success, so it's safe to use directly (`curl -H "Authorization: Bearer $(secagent
token --user)" ...`). Without `--user` it prints the *service* token instead —
unchanged from before this feature existed.

## pi is optional

[pi](https://pi.dev) is the agent *runtime* — the TypeScript coding agent that owns
the loop, tools, and sessions, and that `secagent init` configures a `secrouter`
provider for. secagent's own CLI (`secagent index`/`scan`/`docs build`/`review`/...)
never depends on pi being installed at all: every command above works with `pi`
missing.

If `install.sh` found `npm`, pi is already installed and configured — run it and
select the model `secagent init` registered:

```bash
pi --provider secrouter
```

If you skipped pi (no Node, or you just don't want it), nothing above breaks; install
Node + `npm install -g @earendil-works/pi-coding-agent` later and re-run `secagent
init` to wire it up retroactively. `secagent doctor` reports pi's (and Node's) status
either way, always as a warning, never a failure.

```{note}
`secagent init`'s `models.json` does **not** depend on
`pi/extensions/secrouter-auth.ts`, a separate pi-native extension that drives the same
device-code flow *inside* pi's own `/login` command and stores the result in pi's
`auth.json` instead. That extension is still there and still works if you'd rather
integrate at the pi level directly (see `pi/README.md` "3b. Point pi at SecRouter") —
it's just not what the default onboarding here uses, so pi stays optional and secagent
never has to know anything about pi's `auth.json` schema.
```

## Verify

```bash
secagent doctor            # Python/secagent/pi/Node versions, init done?, logged in?
secagent doctor --probe    # + best-effort reachability of SecRouter and SecSSO
secagent doctor --fix      # pre-create + harden (0700) the token-cache directory
```

`doctor` never fails on a missing `pi`/Node or a not-yet-completed `init`/`login` —
those are warnings, since plenty of legitimate secagent use needs neither (e.g. CI).
It fails only on a real blocker (an unsupported Python version, a broken FIPS/audit
posture, ...).

(how-this-differs-from-a-secdeploy-service-install)=
## How this differs from a SecDeploy/service install

This page is about **one person, one laptop**: no root, no `systemd`, everything
under your `$HOME`, and every request carries *your own* identity because you ran
`secagent login`.

A SecDeploy-managed install (see `secdeploy`'s `fedora-fips` target) is the opposite
shape: secagent runs as an unattended **service** — `secagent chat serve`, `secagent
review serve` — under a dedicated service account (`svc-secagent`), installed to
`/etc/secsuite/` on a managed host, with `SECAGENT_LLM__API_KEY=!secagent token`
(**no** `--user`: the client-credentials service identity, a shared secret provisioned
out of band, never an interactive login). There is no `secagent login` step in that
path at all — a bot has no browser to approve a device code in, and shouldn't have one
requested on its behalf.

Both paths end up calling the same SecRouter, through the same kind of
`!secagent token[...]` indirection, and both are documented in
{doc}`configuration` ("Inference at SecRouter" / the `secsso` section) — they're two
different *identities* for two different *callers*, not two different products.

## Optional extras

The extras below are unrelated to onboarding and install with `pip install
"secagent[...]"` (or add `--with <extra>` args to the `uv tool install` in
`install.sh` if you want them from the start):

`docs`
: Sphinx + `sphinxcontrib-drawio` for the documentation deep-dive (UC1).

`review`
: FastAPI webhook server for the GitLab MR reviewer (UC100).

`tokenizer`
: precise Gemma token counts via `tokenizers`. Falls back to a deterministic
  heuristic when absent, so secagent works air-gapped without downloading tokenizer
  assets.

`clang`
: accurate C/C++ functions + inter-file call map via libclang. Absent → regex symbols.

`csharp`
: accurate C# functions + call map via tree-sitter (no .NET SDK). Absent → regex
  symbols. For fully-resolved semantic C# analysis, the heavy Roslyn backend runs in an
  optional container (`make analyzer-dotnet`); see {doc}`design/heavy-analysis-pipeline`.

`rust`
: accurate Rust functions + call map via the tree-sitter grammar (no Rust toolchain,
  no built project). Absent → regex symbols. For fully-resolved cross-crate calls and
  the trait/impl graph, the heavy rust-analyzer backend runs in an optional container
  (`make analyzer-rust`); see {doc}`design/heavy-analysis-pipeline`.

## Contributing to secagent itself

The steps above install secagent as a *user*. To work on secagent's own source:

```bash
git clone https://github.com/secrouter/secagent
cd secagent
make dev             # editable install with all extras (needs Python 3.11+)
make verify          # ruff + mypy + pytest + secagent doctor
```

`make dev` picks a suitable interpreter automatically (handy since the default
`python3` on macOS is often an older system build, e.g. 3.9). Without `make`, point
pip at Python 3.11+ yourself — a virtualenv keeps it isolated:

```bash
python3.11 -m venv .venv && . .venv/bin/activate
pip install -e ".[docs,review,tokenizer,clang,csharp,dev]"
```

## A local model endpoint (no SecRouter yet?)

If you don't have a SecRouter deployment to point at, secagent and pi both talk to
any OpenAI-compatible endpoint directly — no model server is bundled:

```bash
# llama.cpp
llama-server -m gemma-3-12b-it-Q4_K_M.gguf --host 0.0.0.0 --port 8000
# vLLM
vllm serve google/gemma-3-12b-it --port 8000
```

then point `SECAGENT_LLM__BASE_URL` (or `~/.secagent/config.yaml`'s `llm.base_url`) at
it instead of running `secagent init`. See {doc}`configuration`.

## Drawio rendering (optional)

Inline Draw.io rendering for generated docs needs the `drawio` desktop binary plus
`xvfb-run` on headless hosts. Without them, secagent still emits the `.drawio` sources
and the documentation still builds — only the pre-rendered images are skipped. The
container image bundles both (see {doc}`fips` and the project Dockerfiles).
