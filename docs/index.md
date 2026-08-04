# secagent

**Containerized pi-agents for local (Gemma) models — FIPS-compatible.**

secagent pairs the [**pi** coding agent](https://pi.dev) (the agentic loop) with a
context-frugal toolset built for *local* models (the Gemma family). **pi is the
runtime** — it owns the loop, tools (read/write/edit/bash), sessions, and provider
selection. **secagent is what pi drives**: an *affordance engine* that pre-computes
compact, content-addressed representations of a codebase so the agent works from a
minimal, budget-bounded context instead of raw source.

Two use cases ship today, both built on the same affordance engine:

- **Docs deep-dive** — pi loops over a codebase using the affordance tools, then
  builds a comprehensive Sphinx site with Draw.io architecture diagrams.
- **GitLab MR review** — reviews new merge requests, posts an initial comment, and
  replies in-thread when @-mentioned, steered by an editable persona.

```{admonition} Why "affordances"?
:class: tip
A Gemma-2 model has an 8k context window; even Gemma-3 (128k) reasons better with
less, more structured input. secagent spends cheap, cached, deterministic passes
turning a repo into high-signal artifacts (structure map, file summaries, IO map,
symbol index), then a budget-aware retriever assembles only what a task needs.
```

```{toctree}
:maxdepth: 2
:caption: Guide

installation
configuration
running-on-a-project
full-analysis
use-cases
git-scope
gitlab-watch
pi
integrations
architecture
affordances
```

```{toctree}
:maxdepth: 2
:caption: Compliance

fips
cmmc
```

```{toctree}
:maxdepth: 2
:caption: Reference

cli
api
```

## Indices

- {ref}`genindex`
- {ref}`modindex`
