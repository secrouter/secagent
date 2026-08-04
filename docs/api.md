# API reference

Auto-generated from docstrings. The most useful entry points for embedding secagent in
your own tooling.

## Configuration

```{eval-rst}
.. automodule:: secagent.config
   :members: Settings, LLMConfig, GitLabConfig, AffordanceConfig, PersonaConfig, FIPSConfig, load_settings
```

## Affordances

```{eval-rst}
.. automodule:: secagent.affordances.api
   :members: index_repo

.. automodule:: secagent.affordances.queries
   :members:

.. automodule:: secagent.affordances.store
   :members: AffordanceStore

.. automodule:: secagent.affordances.retrieval
   :members: ContextBuilder

.. automodule:: secagent.affordances.models
   :members:
```

## LLM

```{eval-rst}
.. automodule:: secagent.llm.client
   :members: LLMClient, LLMResponse, ToolSpec, ToolCall, LLMError

.. automodule:: secagent.llm.tokenizer
   :members: TokenCounter, count_tokens
```

## Agents

```{eval-rst}
.. automodule:: secagent.agents.docs.agent
   :members: build_docs

.. automodule:: secagent.agents.review.agent
   :members: review_merge_request, review_local_changes, respond_to_mention

.. automodule:: secagent.agents.review.persona
   :members: Persona, load_persona

.. automodule:: secagent.agents.analysis.agent
   :members: analyze_repo

.. automodule:: secagent.agents.analysis.ikos
   :members: parse_ikos_report, run_ikos, ikos_available

.. automodule:: secagent.agents.analysis.models
   :members:

.. automodule:: secagent.agents.scan.agent
   :members: scan_repo, parse_findings

.. automodule:: secagent.agents.scan.rules
   :members: Rule, RuleSet, load_rules, rules_prompt, meets_threshold

.. automodule:: secagent.agents.testgen.agent
   :members: generate_tests
```

## GitLab harness

```{eval-rst}
.. automodule:: secagent.mcp.gitlab_harness
   :members: GitLabClient, build_gitlab_tools, GitLabError
```

## Git scope

See {doc}`git-scope` for the user-facing guide; this is the programmatic surface.

```{eval-rst}
.. automodule:: secagent.gitscope
   :members: GitScopeError, FileChange, ChangeSet, since_base, since_ref,
             working_tree, staged, range, explicit, analyzable, resolve_scope,
             describe_scope, coverage_banner, current_branch
```

## Security / FIPS

```{eval-rst}
.. automodule:: secagent.security
   :members:
```
