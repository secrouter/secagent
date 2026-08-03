# The affordance engine

The affordance engine is secagent's core: it turns a repository into compact,
content-addressed artifacts so a small/local model works from summaries, IO edges, and
slices instead of raw source.

## Artifacts

Structure map
: Pruned directory tree, components (directory groups), language mix, entrypoints, and
  build files. Rendered as a compact outline for the agent.

File summaries
: Per file: a one-line purpose (heuristic, optionally refined by the LLM and cached by
  content hash), key symbol signatures, and detected IO signals (endpoints, outbound
  calls, env vars, datastores, message queues).

IO map
: Directed edges between components and externals: internal imports, exposed HTTP
  endpoints, outbound calls, datastore usage, **message queues / brokers**, and
  environment inputs. Powers the architecture diagrams and the reviewer's
  cross-component reasoning.

  Messaging is a first-class category: secagent detects Kafka, RabbitMQ/AMQP, MQTT,
  ZeroMQ, NATS, Pulsar, ActiveMQ/STOMP, NSQ, AWS SQS/SNS, Azure Service Bus, GCP
  Pub/Sub, MSMQ + .NET buses (MassTransit/NServiceBus/Rebus), nanomsg, and generic
  message-bus / pub-sub structures, across languages. The generated **Data Flow & IO**
  docs page calls them out in a dedicated *Messaging* section — which component uses
  which broker.

Symbol index
: Functions/classes/methods → file + line + signature (Python via `ast`; other
  languages via heuristics).

## Store

A SQLite database plus JSON artifacts under `store_dir` (default `.secagent`), all
content-addressed with SHA-256. Indexing is **incremental**: a file whose hash is
unchanged keeps its existing summary/symbols and is skipped.

## Budget-aware retrieval

Given a task and a token budget (sized to the model's context window via the Gemma
tokenizer), the retriever assembles the smallest useful context: the project outline,
the IO summary, and the most relevant file summaries — never whole files unless
explicitly requested via a bounded slice.

## The query surface

These commands are what pi drives via bash (and what the extension wraps). Each prints
a compact result (text or JSON):

```bash
secagent affordance structure <repo>
secagent affordance io <repo>                      # imports, endpoints, calls, datastores, message queues
secagent affordance components <repo>
secagent affordance plan <repo>                    # components binned by language + tools (UC0)
secagent affordance search <repo> "<query>"
secagent affordance summary <repo> <path>
secagent affordance functions <repo> <path>        # a file's functions: signature + description
secagent affordance calls <repo> [path]            # the inter-file call map
secagent affordance callers <repo> <symbol>        # who calls a function — impact analysis
secagent affordance types <repo> [name]            # declared types + inheritance
secagent affordance summaries <repo> [--raw]       # per-model manifest of generated summaries
secagent affordance cache <repo> [--prune N|--clear]  # LLM cache size / reclaim space
secagent affordance find-symbol <repo> <name>
secagent affordance context <repo> "<query>"
secagent affordance slice <repo> <path> --start N --end M
```

If the store does not exist yet, these commands auto-build a heuristic-only index;
running `secagent index` first (optionally with LLM summaries) gives richer results.

```{important}
Every query above answers from the index, not from the working tree. After editing code,
re-run `secagent index <repo> --no-llm` (incremental, no model calls) — otherwise the
answers describe the last snapshot while looking exactly like current ones. Agents on the
MCP surface have a `reindex` tool for this.
```

Relevance ranking weights query terms by inverse document frequency, so the filler in a
natural-language question ("how do I add a new X to this parser?") cannot outvote the one
term that identifies the answer, and a merely large file cannot win on volume.

### Sizing the assembled block

`affordance context` fills the token budget derived from `llm.context_window`, up to
`affordances.max_context_files` (default 60). The file count used to be fixed at twelve,
which silently became the only constraint — against a 117k-token server, a 14x larger
window produced a 7% larger block because both runs stopped at twelve files.

Set `SECAGENT_LLM__CONTEXT_WINDOW` to what your server actually serves (`secagent doctor
--probe` reads it off the server and tells you), then raise
`SECAGENT_AFFORDANCES__MAX_CONTEXT_FILES` if you want to spend more of it. The ceiling
exists because the goal is the smallest context that answers the question: past some
point extra file summaries bury the relevant ones rather than adding to them.

```{tip}
`read_slice` / `affordance slice` is traversal-guarded: a path resolving outside the
repository root is rejected.
```
