# sample_repo

A tiny two-service system used as a fixture for secagent's affordance engine and
documentation/review agents.

- `services/api` — FastAPI HTTP service (reads/writes users, enqueues jobs).
- `services/worker` — background worker consuming a Redis queue.
- `common` — shared domain models.
