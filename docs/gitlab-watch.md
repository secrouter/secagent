# Watching a GitLab repository

This guide shows how to run the {doc}`UC100 review agent <use-cases>` as a continuous
loop against a GitLab project: the agent posts an initial review on every new merge
request and replies in-thread whenever it is @-mentioned. It covers the GitLab-side
infrastructure (bot user, access token, webhook) as well as the secagent-side service
and the webhook-free polling alternative.

```{tip}
Run the {doc}`docs deep-dive (UC1) <use-cases>` against the same checkout first
(`secagent docs build <repo>`). It populates the affordance store, so reviews reason
about cross-component impact from the first MR instead of building a lightweight store
on demand.
```

## How the loop works

secagent offers two ways to watch a project. Both route every event through the same
handler, so the review behaviour is identical — pick based on whether your GitLab
instance can reach the secagent service.

| Mode | Command | Use when |
|------|---------|----------|
| **Webhook** (push, event-driven) | `secagent review serve` | GitLab can open an outbound connection to secagent (same network, or reachable over TLS) |
| **Polling** (pull, interval) | `secagent review poll` | Air-gapped / one-way networks where GitLab cannot deliver webhooks |

In both modes:

* a **new or reopened MR** triggers one structured initial review comment;
* a **comment mentioning the bot** (`@secagent-bot`) triggers an in-thread reply;
* the agent never replies to its own comments.

## Step 1 — Create a bot user and access token (GitLab side)

The agent authenticates to the GitLab REST API as a dedicated user. Using a separate
bot account (rather than a human's token) keeps the audit trail clean and lets you
scope and rotate credentials independently.

1. **Create the bot user.** As an instance admin, create a normal user — e.g.
   `secagent-bot` (**Admin → Users → New user**). On GitLab SaaS, create a standard
   account or use a [project/group access token](https://docs.gitlab.com/ee/user/project/settings/project_access_tokens.html),
   whose bot identity is provisioned automatically.
2. **Grant repository access.** Add the bot to the project (or its parent group) with
   at least the **Developer** role — required to post notes and reply to discussions.
   Reporter can read MRs but cannot comment.
3. **Mint an access token** for the bot with the **`api`** scope
   (**User settings → Access tokens**, or the project/group access-token screen).
   `read_api` is *not* sufficient because the agent posts comments. Set an expiry and
   plan to rotate it.

```{important}
Treat the token as a secret. Provide it via the `SECAGENT_GITLAB__TOKEN` environment
variable or a mounted secret — never commit it or place it in `secagent.yaml`. secagent
redacts tokens from logs, but a leaked token grants `api` access as the bot.
```

Record the matching secagent configuration (`gitlab` section of `secagent.yaml`, or the
equivalent `SECAGENT_GITLAB__*` env vars — see {doc}`configuration`):

```yaml
gitlab:
  url: "https://gitlab.example.com"   # your instance (no trailing /api)
  token: ""                            # set via SECAGENT_GITLAB__TOKEN
  bot_username: "secagent-bot"           # must match the bot's username for @-mentions
  verify_tls: true
```

`bot_username` must equal the bot account's GitLab username: the @-mention trigger
looks for `@<bot_username>` in comment bodies.

## Step 2a — Configure the webhook (push mode)

Run the receiver where GitLab can reach it. Terminate TLS at secagent or at an ingress
in front of it (see [Securing the endpoint](#securing-the-endpoint)):

```bash
export SECAGENT_GITLAB__TOKEN="glpat-…"            # bot api token
export SECAGENT_GITLAB__WEBHOOK_SECRET="$(openssl rand -hex 32)"  # shared secret
secagent review serve --host 0.0.0.0 --port 8080
```

Then, in the project, go to **Settings → Webhooks → Add new webhook** and configure:

| Field | Value |
|-------|-------|
| **URL** | `https://<secagent-host>:8080/webhook` |
| **Secret token** | the value of `SECAGENT_GITLAB__WEBHOOK_SECRET` |
| **Trigger: Merge request events** | ✅ enabled (new/reopened MRs) |
| **Trigger: Comments** | ✅ enabled (@-mention replies) |
| **SSL verification** | ✅ enabled (keep on; use a CA-trusted cert) |

GitLab sends the secret in the `X-Gitlab-Token` header; secagent compares it in constant
time and rejects (`401`) any request whose token does not match. Leave all other
triggers off — the agent ignores them, and disabling them avoids needless traffic.

Use **Test → Merge request events** on the webhook page to send a sample delivery, then
check the health endpoint and the secagent logs:

```bash
curl -fsS https://<secagent-host>:8080/healthz
# {"status":"ok","persona":"default.yaml"}
```

## Step 2b — Polling fallback (pull mode)

For instances that cannot deliver webhooks, poll instead. Set the interval and run
`review poll`, which reviews every open MR it has not seen yet on each pass:

```yaml
gitlab:
  poll_interval_s: 120     # seconds between passes (min 5; default 60 when unset)
```

```bash
secagent review poll group/project --repo /path/to/checkout
```

`--once` runs a single pass and exits — ideal for a cron job or a scheduled CI task
when you do not want a long-lived process:

```bash
secagent review poll group/project --repo /repo --once
```

Each tick records the merge requests it has reviewed in `gitlab.poll_state_file`
(`.secagent/review-seen.json` under `--repo`, else the working directory). That file is what
stops cron from reposting: without it every invocation starts blank, every open MR looks
new, and a second full review is published on each run. Put it on storage that survives a
container restart. An MR whose review fails is deliberately *not* recorded, so the next
tick retries it rather than leaving it silently unreviewed.

```{note}
Polling reviews **open MRs** as they appear; it does not currently watch for new
comments (the @-mention reply trigger is webhook-only). Prefer the webhook whenever
GitLab can reach secagent; reserve polling for one-way / air-gapped networks.
```

## Step 3 — Run it as a service

The webhook receiver is a long-running process. Two common deployments:

### Container (compose)

The bundled compose file ships a `review` service:

```bash
docker compose -f docker/docker-compose.yml up review
```

Supply `SECAGENT_GITLAB__TOKEN`, `SECAGENT_GITLAB__WEBHOOK_SECRET`, and `SECAGENT_LLM__*`
through the environment or a secret store; mount the project checkout if you want
affordance-aware reviews. See {doc}`installation`.

### systemd

```ini
# /etc/systemd/system/secagent-review.service
[Unit]
Description=secagent GitLab review webhook
After=network-online.target
Wants=network-online.target

[Service]
# FIPS hosts: run Node/Python against validated system OpenSSL.
Environment=SECAGENT_GITLAB__TOKEN=glpat-…
Environment=SECAGENT_GITLAB__WEBHOOK_SECRET=…
Environment=SECAGENT_LLM__BASE_URL=http://gemma-host:8000/v1
ExecStart=/usr/local/bin/secagent review serve --port 8080 \
    --tls-cert /etc/secagent/server.crt --tls-key /etc/secagent/server.key
Restart=on-failure
# Least privilege
DynamicUser=yes
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload && systemctl enable --now secagent-review
```

For the polling variant, either point `ExecStart` at `secagent review poll <project>`
(long-running) or pair `secagent review poll <project> --once` with a `systemd` timer or
cron entry.

(securing-the-endpoint)=
## Securing the endpoint

The webhook receives untrusted input from the network, so harden it (these map to the
CMMC-4 controls described in {doc}`cmmc`):

* **Shared secret** — set `SECAGENT_GITLAB__WEBHOOK_SECRET` and the matching webhook secret
  token. This is enforced, not advised: `review serve` **refuses to start** without one,
  because the token check used to be skipped whenever the secret was empty — so forgetting
  this variable served an endpoint that accepted any request and posted reviews to any
  project the token could reach. `gitlab.webhook_allow_unauthenticated` is the explicit
  opt-out for deployments fronted by an authenticating proxy.
* **TLS / mTLS** — serve over HTTPS, and add a client CA to require client
  certificates (mutual TLS). GitLab does not present a client cert, so use mTLS only
  behind an ingress/proxy that does:

  ```bash
  secagent review serve --tls-cert server.crt --tls-key server.key --tls-ca client-ca.crt
  ```

* **Source-IP allow-list** — restrict callers to your GitLab egress addresses:

  ```yaml
  gitlab:
    webhook_allowed_ips: ["203.0.113.10", "203.0.113.11"]
  ```

* **Egress policy** — secagent enforces TLS and an optional host allow-list on its own
  outbound calls (to GitLab and the LLM endpoint); see `network` in
  {doc}`configuration`.
* **Audit** — every review and reply is recorded in the tamper-evident audit log
  (`secagent audit verify`); see {doc}`cmmc`.

## Tuning the reviewer

What the agent says — stance, verbosity, focus areas, comment limits — is governed by
the editable persona file (`config/alignment/*.yaml`), reloaded per review with no
restart. See {doc}`use-cases` for the persona schema.
