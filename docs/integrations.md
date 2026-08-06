# Using secagent from a coding agent

secagent exposes its affordances two ways:

| surface | for | tools |
|---|---|---|
| **MCP server** (stdio) | any MCP client — Kilo Code, OpenCode, … | 14 |
| **pi extension** (`pi/extensions/secagent.ts`) | the pi coding agent | 13 |

Both cover the same ground. The MCP server is the portable one — if your agent speaks
MCP, it needs no secagent-specific code.

**Context compression.** secagent also integrates [LeanCTX](leanctx.md) — on by default, locked
down — which adds `ctx_*` compression tools to pi and shrinks model requests (the agent's and
secagent's own chat/review calls) before they reach SecRouter. See [LeanCTX](leanctx.md) for the
security posture (loopback-only, no telemetry/phone-home, no CUI at rest by default).

## Install

```bash
pip install "secagent[clang]"        # clang extra = accurate C/C++ symbols + call map
secagent doctor                      # confirm the install; --probe also tests the model
```

`secagent doctor` reports which analysis backends are present. Without `libclang` a C/C++
repo indexes to zero functions and an empty call map, so check this first.

## Index the repository once

```bash
secagent index /path/to/repo --no-llm     # structure, symbols, IO map, call map (seconds)
secagent index /path/to/repo              # ...plus an LLM-written purpose per file (slower)
```

Re-indexing is incremental. Agents can also let `secagent` auto-index on first query, but
an explicit first run makes the cost visible rather than surprising.

---

## Kilo Code

MCP servers go under the `mcp` key of `kilo.jsonc` (JSONC — comments allowed) — globally at
`~/.config/kilo/kilo.jsonc`, or per project as `kilo.jsonc` / `.kilo/kilo.jsonc` in the
workspace root.

```json
{
  "mcp": {
    "secagent": {
      "type": "local",
      "command": ["secagent", "mcp", "affordances"],
      "enabled": true,
      "timeout": 30000
    }
  }
}
```

## OpenCode

Same shape, different file: `~/.config/opencode/opencode.json` globally, or
`opencode.json` / `opencode.jsonc` (comments allowed) in the workspace root.

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "secagent": {
      "type": "local",
      "command": ["secagent", "mcp", "affordances"],
      "enabled": true
    }
  }
}
```

### Notes for both

- **No repository path is needed.** The server defaults to its working directory, which
  MCP clients set to the workspace root. Pass an explicit path
  (`["secagent", "mcp", "affordances", "/abs/path"]`) only for a repo outside the workspace,
  or set `cwd` where the client supports it.
- **Raise the timeout.** The default (5–10s depending on client) is fine for queries
  against an existing index, but the *first* call may trigger an auto-index. Either index
  ahead of time or allow ~30s.
- **Point secagent at your model** if you want LLM-written summaries, via
  `SECAGENT_LLM__BASE_URL` / `SECAGENT_LLM__MODEL` in the config's `environment` block —
  any OpenAI-compatible endpoint works, including a gateway such as SecRouter
  (`SECAGENT_LLM__BASE_URL=https://secrouter.<domain>:47002/v1`, `SECAGENT_LLM__API_KEY`
  carrying the bearer token; see {doc}`configuration`). The affordance tools themselves do
  not call a model — only indexing does.
- **Check `SECAGENT_LLM__CONTEXT_WINDOW` against what your server serves.** The default
  (131072) suits a modern local Gemma; lower it for a smaller model. `secagent doctor
  --probe` reads the real number off the server and flags a mismatch either way — see
  below for why it matters more than it looks.

---

## Sizing the context window

```bash
secagent doctor --probe        # reports configured vs. actually served
```

`llm.context_window` is not just how much secagent will *use*. The recovery path for
reasoning models derives its ceiling from it, so leaving it small breaks more than it
throttles:

> `llm.context_window=8192` but the server serves `117976` tokens for
> `google/gemma-4-26b-a4b-qat` — secagent is using 6% of the available context, and
> empty-content retries are capped at 4096 tokens. Set `SECAGENT_LLM__CONTEXT_WINDOW=117976`

(That was the old 8192 default. The check reports the opposite case too: a window whose
prompts would exceed what the server serves.)

**Why the retry ceiling matters.** A reasoning model emits `reasoning_content` before any
`content`. If the output budget runs out first, the call returns HTTP 200 with an EMPTY
answer — which every consumer reads as "nothing to report". secagent retries at a larger
budget, but that budget is bounded by the window. Measured against a local Gemma-4 on an
ordinary "analyse this source" prompt:

| output budget | result |
|---|---|
| 8192 | empty — budget consumed by reasoning |
| 12288 | empty |
| 16000 | **answered** (and stopped on its own) |

So an 8192 window caps the retry at 4096 and *no* amount of tuning `max_output_tokens`
can help — the run just reports every file as unanalysable. Raising the window fixes it.

The escalated retry is bounded (32768) rather than "as much as the window allows". A
larger cap is free only while the model converges: measured, a 3894-byte header consumed
a 58988-token budget over 10.5 minutes and still produced nothing. The headroom bought no
recovery and cost the time. Some files simply defeat a given model, and that is reported
rather than waited out.

## The tools

| tool | answers |
|---|---|
| `get_structure` | what is this project — components, languages, entrypoints |
| `list_components` | the cohesive units and what each is for |
| `get_io_map` | imports, HTTP endpoints, outbound calls, datastores, sockets |
| `search_files` | which files are relevant to a question |
| `get_file_summary` | what one file is for, plus its IO signals |
| `list_functions` | a file's API without reading the file |
| `find_symbol` | where a function / class / macro / typedef lives |
| `find_callers` | **what depends on this** — check before you change it |
| `get_call_map` | which file calls into which, via which functions |
| `list_types` | declared types and their inheritance |
| `get_analysis_plan` | components binned by language + which tools to run |
| `read_file_slice` | a bounded, traversal-guarded slice of a real file |
| `context_for` | a budget-bounded context block for a question |
| `reindex` | pick up your edits — see the staleness note below |

The point of all of this is to answer questions *without* reading whole files, so a
local model's context stays small.

## Verifying the connection

Drive the server directly — this is exactly what the editor does:

```bash
cd /path/to/repo
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
  | secagent mcp affordances
```

You should see `secagent-affordances` and 14 tools. If the client shows fewer, it is
filtering them; if it shows none, check that `secagent` is on the PATH *the editor sees*
(GUI editors often do not inherit a shell PATH — use an absolute path to the binary).

## Staleness — the one thing to get right

secagent answers from an **index, never from disk**. Edit a file and every tool keeps
returning it as it was, in the ordinary shape, with nothing marking the answer stale.

So after editing, call **`reindex`**. It is incremental and makes no model calls, so it
costs about a second on a repo of a few hundred files:

```bash
secagent index <repo> --no-llm     # the same thing from a shell
```

The MCP surface carries one tool the pi extension does not, and this is it: pi always has
a shell to run `secagent index` in, while an MCP client may have no shell at all. That is
why editing through Kilo Code or OpenCode and then querying used to serve stale answers
with no way to fix them from inside the editor.

Where secagent cannot answer reliably it now says so rather than returning a confident
empty result: `find_callers` warns when the call map is incomplete, `list_functions`
marks a truncated list, and `secagent scan` reports files it could not analyse instead of
implying they were clean.
