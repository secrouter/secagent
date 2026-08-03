# Installation

secagent is the Python toolset; [pi](https://pi.dev) is the agent runtime that drives
it. Install both.

## Python toolset

**Requires Python 3.11+.** secagent is not yet published to PyPI, so install it from
source ([below](#from-source)). Note that the default `python3` on macOS is often an
older system build (e.g. 3.9); use an explicit `python3.11` (or newer) interpreter, or
`make`, which resolves a suitable one.

Extras:

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

`dev`
: ruff, mypy, pytest for development.

### From source

```bash
git clone https://github.com/secrouter/secagent
cd secagent
make dev             # editable install with all extras (uses Python 3.11+)
make verify          # ruff + mypy + pytest + secagent doctor
```

`make dev` runs the editable install with a suitable interpreter. To install without
`make`, point pip at Python 3.11+ explicitly — a virtualenv keeps it isolated:

```bash
python3.11 -m venv .venv && . .venv/bin/activate
pip install -e ".[docs,review,tokenizer,clang,csharp,dev]"
```

## pi (the agent runtime)

```bash
npm install -g @earendil-works/pi-coding-agent   # or: curl -fsSL https://pi.dev/install | sh
```

See {doc}`pi` for loading the secagent extension and pointing pi at a local Gemma
model.

## A local model endpoint

secagent and pi both talk to any OpenAI-compatible endpoint — no model server is
bundled:

```bash
# llama.cpp
llama-server -m gemma-3-12b-it-Q4_K_M.gguf --host 0.0.0.0 --port 8000
# vLLM
vllm serve google/gemma-3-12b-it --port 8000
```

## Verify

```bash
secagent doctor          # FIPS + dependency self-check
secagent doctor --probe  # also probes the configured LLM endpoint
```

## Drawio rendering (optional)

Inline Draw.io rendering for generated docs needs the `drawio` desktop binary plus
`xvfb-run` on headless hosts. Without them, secagent still emits the `.drawio` sources
and the documentation still builds — only the pre-rendered images are skipped. The
container image bundles both (see {doc}`fips` and the project Dockerfiles).
