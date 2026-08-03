# Running under pi

**pi is the agent runtime; secagent is the context-frugal toolset it drives.** pi owns
the loop, tools (read/write/edit/bash), sessions, and provider selection. secagent adds
the affordance engine, docs generation, and GitLab review. pi has no built-in MCP, so
the integration is via a **Skill** (a CLI + README the agent uses through bash) and an
optional **TypeScript extension** (registers first-class tools and slash commands).

The integration lives under `pi/` in the repository.

## 1. Point pi at a local Gemma model

Copy `pi/models.example.json` to `~/.pi/agent/models.json` and set `baseUrl`:

```json
{
  "providers": {
    "local-gemma": {
      "api": "openai",
      "baseUrl": "http://localhost:8000/v1",
      "apiKey": "not-needed",
      "models": [
        { "id": "gemma-3-12b-it", "name": "Gemma 3 12B (local)", "contextWindow": 131072 }
      ]
    }
  }
}
```

```bash
pi --provider local-gemma --model gemma-3-12b-it
```

## 2. Load the secagent extension

```bash
pi --extension ./pi/extensions/secagent.ts
export SECAGENT_REPO=/path/to/repo     # which repo the tools operate on (default: cwd)
```

The extension registers these tools so the model reads summaries/slices instead of
whole files:

| Tool | Backed by |
|------|-----------|
| `secagent_structure` | `secagent affordance structure` |
| `secagent_io_map` | `secagent affordance io` |
| `secagent_search` | `secagent affordance search` |
| `secagent_file_summary` | `secagent affordance summary` |
| `secagent_find_symbol` | `secagent affordance find-symbol` |
| `secagent_context` | `secagent affordance context` |
| `secagent_read_slice` | `secagent affordance slice` |
| `secagent_plan` | `secagent affordance plan` (UC0 binning) |

…plus slash commands `/secagent-analyze-all` and `/secagent-plan` (UC0 full analysis),
`/secagent-docs`, `/secagent-review`, `/secagent-testgen`, `/secagent-scan`, `/secagent-analyze`.
There are two Skills: `skills/secagent/` (the affordance recipes) and
`skills/secagent-analysis/` (the UC0 full-analysis playbook).

## 3. Or use the Skill (no TypeScript)

`pi/skills/secagent/SKILL.md` describes the `secagent` CLI so pi can use it through its
bash tool — no extension required.

```{note}
The `~/.pi/agent/models.json` schema and the extension's `registerTool` /
`registerCommand` field names follow pi's documented extension API; adjust to your
installed pi version if it differs.
```

## FIPS note

pi runs on Node.js. Run it with `--enable-fips` (the container sets
`NODE_OPTIONS=--enable-fips`) on a FIPS host so pi uses the validated OpenSSL module.
All of secagent's hashing/TLS/secret handling stays in the Python layer. See {doc}`fips`.
