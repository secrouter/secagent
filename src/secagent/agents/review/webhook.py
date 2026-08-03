"""GitLab webhook receiver (FastAPI) with a polling fallback.

Verifies the shared ``X-Gitlab-Token`` secret, then routes Merge Request and Note
events to the review triggers. For instances that cannot deliver webhooks (air-gapped),
``poll_open_merge_requests`` provides a pull-based alternative.
"""

from __future__ import annotations

import hmac
import json
import logging
import time
from pathlib import Path
from typing import Any

# FastAPI is an optional extra; this module is only imported when the review server
# is actually used (CLI `review serve` or tests), so a top-level import is fine — and
# it is required so FastAPI can resolve the (string-ified) `Request` annotation.
from fastapi import FastAPI, Header, HTTPException, Request

from ...config import Settings
from .triggers import dispatch_event

log = logging.getLogger(__name__)


class WebhookAuthError(RuntimeError):
    """The receiver was asked to serve without a way to authenticate callers."""


def create_app(settings: Settings, repo: str | Path | None = None):
    """Build the FastAPI app.

    Raises :class:`WebhookAuthError` when no ``gitlab.webhook_secret`` is configured.
    The check used to be ``if secret and not compare_digest(...)`` — with the default
    empty secret the whole comparison was skipped and every request passed, so a missing
    env var produced an endpoint anyone who could reach the port could use to make the
    bot post to any project the token had access to. Failing at startup is the same
    fail-loud contract ``enforce_fips_policy``/``enforce`` already apply on this path.
    """
    if not settings.gitlab.webhook_secret and not settings.gitlab.webhook_allow_unauthenticated:
        raise WebhookAuthError(
            "refusing to serve the review webhook with no gitlab.webhook_secret: the "
            "endpoint would accept any request and post reviews to any project the "
            "GitLab token can reach. Set SECAGENT_GITLAB__WEBHOOK_SECRET to the same "
            "value as the GitLab webhook's secret token, or set "
            "gitlab.webhook_allow_unauthenticated=true if you really do mean an open "
            "endpoint (e.g. behind an authenticating proxy).")
    app = FastAPI(title="secagent-review", version="0.1.0")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "persona": Path(settings.persona.profile).name}

    @app.post("/webhook")
    async def webhook(
        request: Request,
        x_gitlab_event: str = Header(default=""),
        x_gitlab_token: str = Header(default=""),
    ) -> dict[str, Any]:
        # Source-IP allow-list (defense-in-depth; pair with mTLS at the ingress).
        allowed = settings.gitlab.webhook_allowed_ips
        if allowed:
            client_ip = request.client.host if request.client else ""
            if client_ip not in allowed:
                raise HTTPException(status_code=403, detail="source IP not allowed")
        # Constant-time shared-secret check to avoid timing oracles. `secret` cannot be
        # empty here unless the operator set webhook_allow_unauthenticated at startup, so
        # this `if` is that explicit opt-out — not a default that disables auth.
        secret = settings.gitlab.webhook_secret
        if secret and not hmac.compare_digest(x_gitlab_token, secret):
            raise HTTPException(status_code=401, detail="invalid webhook token")
        payload = await request.json()
        try:
            report = dispatch_event(settings, x_gitlab_event, payload, repo=repo, post=True)
        except Exception as exc:  # never 500 to GitLab; report and move on
            return {"action": "error", "error": str(exc)}
        # Keep the response small; the full review was posted to GitLab, not returned.
        return {"action": report.get("action"), "reason": report.get("reason"),
                "mr_iid": report.get("mr_iid"), "posted": report.get("posted")}

    return app


def serve(settings: Settings, host: str = "0.0.0.0", port: int = 8080,
          repo: str | Path | None = None, *,
          tls_certfile: str | None = None, tls_keyfile: str | None = None,
          tls_ca_certs: str | None = None) -> None:
    """Run the webhook receiver.

    Supplying ``tls_certfile``/``tls_keyfile`` serves over HTTPS; additionally
    supplying ``tls_ca_certs`` enables **mTLS** (the client certificate is required and
    verified against the CA) — CMMC-4.
    """
    import ssl

    import uvicorn

    from ...netpolicy import enforce
    from ...security import enforce_fips_policy

    enforce_fips_policy(settings.fips.require_fips, settings.fips.allow_non_fips)
    enforce(settings)  # fail fast if endpoints violate the egress policy
    app = create_app(settings, repo=repo)
    ssl_kwargs: dict[str, Any] = {}
    if tls_certfile and tls_keyfile:
        ssl_kwargs["ssl_certfile"] = tls_certfile
        ssl_kwargs["ssl_keyfile"] = tls_keyfile
        if tls_ca_certs:
            ssl_kwargs["ssl_ca_certs"] = tls_ca_certs
            ssl_kwargs["ssl_cert_reqs"] = ssl.CERT_REQUIRED  # require client cert (mTLS)
    uvicorn.run(app, host=host, port=port, **ssl_kwargs)


def _load_seen(state_path: Path, project: str) -> set[int]:
    """Read the persisted seen-set for one project, tolerating a missing/corrupt file.

    A state file that cannot be read must not stop the poller — but it must not silently
    read as "nothing seen" either, because that is the double-post bug returning by
    another route. Corruption is logged loudly; the pass then proceeds as a first run.
    """
    try:
        data = json.loads(state_path.read_text())
        return {int(i) for i in data.get(project, [])}
    except FileNotFoundError:
        return set()
    except (OSError, ValueError, AttributeError, TypeError) as exc:
        log.error("review poll state at %s is unreadable (%s); treating this as a first "
                  "pass, which may repost reviews for already-reviewed merge requests.",
                  state_path, exc)
        return set()


def _save_seen(state_path: Path, project: str, seen: set[int]) -> None:
    """Persist this project's seen-set, merging with other projects already recorded."""
    try:
        data = json.loads(state_path.read_text())
        if not isinstance(data, dict):
            data = {}
    except (FileNotFoundError, OSError, ValueError):
        data = {}
    data[project] = sorted(seen)
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = state_path.with_suffix(state_path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(state_path)  # atomic: a crash mid-write must not truncate the set
    except OSError as exc:
        log.error("could not persist review poll state to %s (%s) — the next run will "
                  "not know these merge requests were reviewed and may repost.",
                  state_path, exc)


def poll_open_merge_requests(
    settings: Settings, project: str, *, repo: str | Path | None = None,
    seen: set[int] | None = None, once: bool = False,
    state_path: str | Path | None = None,
    gitlab: Any | None = None, llm: Any | None = None,
) -> set[int]:
    """Poll a project for open MRs and review any not yet seen.

    The pull-based fallback for GitLab instances that cannot deliver webhooks
    (air-gapped networks). Each new open MR is routed through the same
    :func:`dispatch_event` path a webhook would use, so behaviour is identical.

    Returns the updated ``seen`` set. With ``once=True`` it runs a single pass;
    otherwise it loops every ``gitlab.poll_interval_s`` seconds. ``gitlab`` and ``llm``
    clients may be injected (the same instance is reused for every pass) — this is what
    makes the loop testable offline; left unset, a client is built per pass from settings.

    ``state_path`` persists the seen-set across processes, keyed by project. Without it,
    ``--once`` from cron — the pattern our own docs recommend — started every tick with an
    empty set, so every open MR looked new and got a full public review posted again on
    every run. An explicit ``seen`` argument takes precedence and keeps the call purely
    in-memory. (An alternative that needs no storage is to ask ``list_discussions``
    whether the bot has already commented; that costs a request per MR per tick, and
    cannot express "reviewed, and the note was later deleted".)

    One MR failing does not abort the pass. The failure is logged and the MR is left OUT
    of the seen-set so the next tick retries it — marking it seen would turn a transient
    GitLab error into a merge request that is silently never reviewed.
    """
    from ...mcp.gitlab_harness import GitLabClient
    from ...netpolicy import enforce
    from ...security import enforce_fips_policy

    enforce_fips_policy(settings.fips.require_fips, settings.fips.allow_non_fips)
    enforce(settings)  # fail fast if endpoints violate the egress policy

    persist = None if seen is not None or state_path is None else Path(state_path)
    seen = seen if seen is not None else (
        _load_seen(persist, project) if persist else set())
    interval = max(5, settings.gitlab.poll_interval_s or 60)
    while True:
        gl = gitlab or GitLabClient(settings.gitlab)
        try:
            for mr in gl.list_open_merge_requests(project):
                iid = mr.get("iid")
                if iid is None or iid in seen:
                    continue
                payload = {
                    "object_kind": "merge_request",
                    "object_attributes": {"action": "open", "iid": iid},
                    "project": {"id": project},
                }
                try:
                    dispatch_event(
                        settings, "Merge Request Hook", payload,
                        repo=repo, post=True, gitlab=gitlab, llm=llm,
                    )
                except Exception as exc:  # noqa: BLE001 - one MR must not end the pass
                    # The webhook route already wraps this identical call. Without the
                    # same isolation here a single flaky GitLab response killed review
                    # coverage for every other open MR in the project.
                    log.error("review of merge request %s!%s failed: %s — continuing "
                              "with the rest of the pass; it will be retried next tick.",
                              project, iid, exc)
                    continue
                seen.add(iid)
                if persist:
                    # Persist per MR, not per pass: a crash halfway through a pass must
                    # not lose the reviews already posted and repost them next tick.
                    _save_seen(persist, project, seen)
        finally:
            if gitlab is None:
                gl.close()
        if once:
            return seen
        time.sleep(interval)
