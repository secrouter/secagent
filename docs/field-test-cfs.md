---
orphan: true
---

<!-- Standalone validation report, intentionally orphaned (kept out of the Sphinx
     toctree) so it does not trip the `-W` docs build. Read it directly on the repo. -->

# Field test: pi-driven workflow + docs generation on NASA cFS

A from-scratch validation of secagent's two integration paths against a large, real C
codebase — the [NASA core Flight System (cFS)](https://github.com/nasa/cFS) — using a
**local Gemma model**. The goal was to exercise the *pi-driven* workflow (pi loads the
secagent extension and the model drives the affordance tools) and the *full docs
generation* (UC1), and to record what worked, what broke, and the fixes.

This report is reproducible: every command is listed, and the bugs it found are linked
to the commits that fix them.

## TL;DR

| Path | Result |
|------|--------|
| pi loads the secagent extension, model calls the `secagent_*` tools | ✅ works |
| pi non-interactive run summarizes cFS architecture (Gemma) | ✅ accurate |
| Full UC1 docs build (summaries → svg diagrams → Sphinx) | ✅ runs end-to-end |
| Gemma actually *contributing* prose to the docs | ❌→✅ (needed a fix) |

**Bugs found and fixed**

1. `pi/models.example.json` used `"api": "openai"`, which **pi 0.80.x rejects** (`No API
   provider registered for api: openai`). Correct value: **`"openai-completions"`**.
2. secagent's docs LLM calls were capped at **80 tokens** (file summaries) / **400**
   (page prose). A **reasoning** model spends its whole output budget on hidden
   reasoning before emitting `content`, so those caps returned empty and secagent
   **silently fell back to heuristics** — the build "succeeded" but the model
   contributed nothing. Raised to **768 / 1500**.

**Known limitations observed** (not fixed here): secagent's C symbol extraction is weak
(`find-symbol` misses real functions); the Gemma reasoning model is unreliable on
pinpoint lookups (hallucinates specific file:line); and a few large-context LLM calls
still fall back even after the budget bump.

## Environment

- Host: macOS, Docker via **Colima** (Lima VM, aarch64). Note: Colima mounts the
  user's home dir but **not** `/private/tmp` — repos under test must live under
  `/Users/...` to be visible inside containers.
- Model endpoint (OpenAI-compatible, LM Studio): `http://192.168.100.197:11434/v1`,
  models `google/gemma-4-26b-a4b-qat` (used here), `openai/gpt-oss-20b`,
  `text-embedding-nomic-embed-text-v1.5`.
- pi: `@earendil-works/pi-coding-agent@0.80.2` (a real npm package).

## Setup

```bash
# 1. Clone cFS with submodules (the real code lives in submodules), under $HOME so
#    Colima can mount it.
git clone --depth 1 --recurse-submodules --shallow-submodules -j4 \
    https://github.com/nasa/cFS.git ~/secagent/cFS
#    -> 94 MB, 2802 C/H files

# 2. Build a test image with both runtimes: Node (pi) + Python 3.11 (secagent) + the
#    secagent CLI, then add pi. (pi installs fine WITHOUT NODE_OPTIONS=--enable-fips;
#    that flag is what blocked it in the FIPS image on a non-FIPS host.)
#    Base = a Debian python+secagent image; then: npm install -g @earendil-works/pi-coding-agent

# 3. Index cFS (heuristic, no LLM) so the affordance tools have a store to read.
secagent index ~/secagent/cFS            # 4432 files, 58 components, 159 io_edges, 640k LOC, ~7s
```

`secagent affordance ...` reads an existing store; it does **not** auto-index, so step 3
is required before the pi tools return anything.

## Test 1 — pi-driven affordance workflow

Point pi at the Gemma endpoint via `~/.pi/agent/models.json` (note the corrected
`api`):

```json
{ "providers": { "local-gemma": {
    "api": "openai-completions",
    "baseUrl": "http://192.168.100.197:11434/v1",
    "apiKey": "not-needed",
    "models": [ { "id": "google/gemma-4-26b-a4b-qat", "contextWindow": 32768 } ]
} } }
```

Run pi **non-interactively** (`-p`) with the secagent extension, against cFS:

```bash
SECAGENT_REPO=/repo SECAGENT_AFFORDANCES__STORE_DIR=/store \
pi -p "Use the secagent_structure tool, then secagent_io_map, to describe this codebase's
       architecture (main components and how they interact) in 5-8 sentences." \
   --extension pi/extensions/secagent.ts \
   --provider local-gemma --model gemma-4-26b-a4b-qat \
   --thinking low --no-session
```

**Result (56 s, exit 0).** pi loaded the extension, the model called the `secagent_*`
tools (confirmed via `toolCall`/`toolResult` entries under `--mode json`), and produced
an accurate summary:

> This codebase is a NASA Core Flight System (cFS) bundle… The central component is the
> Core Flight System (`cfe/`)… applications within `apps/` (command handling,
> telemetry)… an OS Abstraction Layer (`osal/`)… the Platform Specific Package
> (`psp/`)… a layered hierarchy: applications → cFS core → OSAL → PSP → hardware…
> tools in `tools/` such as the EDS.

**Faithfulness caveat.** A follow-up prompt asking for a specific symbol's location
(`secagent_find_symbol CFE_TBL_Register`) exposed two issues: secagent's C symbol index
returned *no match* (or junk symbols named `undefined`), and the model — rather than
reporting that — **hallucinated** `cfe_tbl_api.c:48` (plausible from training), retried
the tool 8×, and once emitted confused output. The integration is sound; the model's
adherence to tool output on pinpoint tasks is the weak link.

## Test 2 — full docs generation (UC1)

This is what `/secagent-docs` runs. Scoped to one real component (the **File Manager**
app, `apps/fm`) to keep the per-file LLM cost tractable.

```bash
SECAGENT_LLM__BASE_URL=http://192.168.100.197:11434/v1 \
SECAGENT_LLM__MODEL=google/gemma-4-26b-a4b-qat \
SECAGENT_LLM__CONTEXT_WINDOW=16384 SECAGENT_LLM__MAX_OUTPUT_TOKENS=3072 \
SECAGENT_DIAGRAMS__RENDERER=svg \
secagent docs build ~/secagent/cFS/apps/fm -o ./cfs-fm-docs
#    -> 94 files indexed (67 C), 15 components, svg diagrams, 8-page Sphinx site
```

### The bug it caught

The **first** run finished in 170 s with `sphinx.ok: true` — but Gemma contributed
**nothing**: the overview was the hardcoded fallback string, and every file summary was
the C file's `*****` banner comment. 170 s ÷ 94 files ≈ 1.8 s/file is far too fast for
the 26B reasoning model to have actually summarized anything.

Direct probe of the endpoint:

```text
max_tokens= 400 -> finish=length completion=400 content=''       # empty!
max_tokens=1500 -> finish=stop   completion=538 content='A flight-software File
                                                          Manager application provides…'
```

**Root cause.** The model emits hidden `reasoning_content` first and only then
`content`. secagent capped its docs calls at `max_tokens=80` (file summaries) and `400`
(page prose), so the reasoning consumed the entire budget and `content` came back
empty — and secagent silently substituted its heuristic (first comment line / fallback
text). On secagent's own (Python) repo this was masked because the heuristic picks up the
module docstring, which *looks* like a model summary.

**Fix.** Raise the caps to give reasoning headroom (plain models stop early, so it
costs them nothing): `file_summary.py` 80 → **768**, `outline.py` `_prose` 400 →
**1500**.

### After the fix

The **re-run** took 543 s (3× longer — the model is now actually working) and produced
real, grounded prose:

> **Overview:** "The `apps/fm` project is a C-based software application… components for
> configuration, flight software (fsw) source and header files, and a unit testing
> suite with stubs and utilities."
>
> **fm_app.c:** "implements the main entry point and initialization for the CFS File
> Manager application, which provides onboard file system management services."
>
> **fm_dispatch.c:** "implements the command dispatching logic for the File Manager
> application, including validation of incoming command packet lengths."

~90% of files now carry a Gemma summary. A few large-context calls (the architecture
page, `fm_compression_fslib.c`) still fall back — reasoning exhausts even the larger
budget. The robust long-term fix is to size these proportional to the configured
`max_output_tokens` rather than fixed constants.

## Findings & fixes

| # | Finding | Status |
|---|---------|--------|
| 1 | `models.example.json` `api: openai` rejected by pi 0.80.x → use `openai-completions` | **fixed** |
| 2 | Docs LLM calls (80 / 400 tokens) return empty on reasoning models → silent heuristic fallback | **fixed** (768 / 1500) |
| 3 | A few large-context LLM calls still fall back after the bump | open (size by `max_output_tokens`) |
| 4 | C symbol extraction weak (`find-symbol` misses real functions, emits `undefined`) | open (secagent C support) |
| 5 | Gemma reasoning model hallucinates on pinpoint lookups; unreliable tool adherence | model limitation |
| 6 | Colima doesn't mount `/private/tmp`; repos under test must be under `$HOME` | environment note |

## Reproducing

1. Clone cFS under `$HOME`, build the Node+Python+secagent+pi image, `secagent index` cFS.
2. Write `~/.pi/agent/models.json` with `api: "openai-completions"` and your endpoint.
3. Run the Test 1 and Test 2 commands above.

## Archive contents

The companion archive (`cfs-field-test-outputs.zip`) holds the evidence:

```
README.md                     # this report
pi-models.json                # the working provider config
pi-workflow/
  architecture-summary.txt    # Test 1 model output
  tool-call-verification.txt  # JSON evidence the secagent_* tools were called
docs-build/
  before-fix/build.log        # 170s run; overview.rst/components.rst show the fallback
  before-fix/overview.rst
  before-fix/components.rst
  after-fix/build.log         # 543s run with Gemma contributing
  after-fix/source/           # generated .rst (Gemma prose) + .drawio/.svg diagrams
  after-fix/html/             # rendered Sphinx site (open index.html)
```
