"""Mattermost inbound receiver (FastAPI) — the ``secagent chat serve`` process.

Accepts BOTH Mattermost delivery mechanisms secagent's own transport supports
(deliberately not the ``pi-mattermost`` plugin):

* **Slash commands** (``/secagent ...``) — form-encoded, works in any channel
  including DMs with the bot.
* **Outgoing webhooks** (trigger word, e.g. ``@secagent`` in a channel) — form- or
  JSON-encoded depending on the webhook's own configuration.

Both deliveries carry the invoking user's identity (``user_id``/``user_name``)
directly in the payload — no separate lookup needed — which is what makes UC101's
per-end-user audit trail (``AuditLogger.record_chat``) possible.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import parse_qsl

# FastAPI is an optional extra; this module is only imported when the chat server is
# actually used (CLI `chat serve` or tests) — same rationale as agents/review/webhook.py.
from fastapi import FastAPI, HTTPException, Request

from ..config import Settings
from .mattermost import MattermostClient
from .router import ChatRequest, MattermostAuthError, dispatch_chat_event, verify_token

log = logging.getLogger(__name__)


def create_app(settings: Settings, *, mattermost: MattermostClient | None = None) -> FastAPI:
    """Build the FastAPI app.

    Raises :class:`MattermostAuthError` when no ``mattermost.webhook_secret`` is
    configured (and ``webhook_allow_unauthenticated`` was not explicitly set) — the
    identical fail-closed contract ``agents/review/webhook.create_app`` already
    applies to the GitLab receiver: without it, a missing env var would produce an
    endpoint that accepts and acts on any request.

    ``mattermost`` may be injected (tests use this to avoid a live Mattermost server);
    left unset, a fresh :class:`MattermostClient` is built per request from settings,
    matching how ``agents/review/webhook`` builds a fresh ``GitLabClient`` per call.
    """
    mm_cfg = settings.mattermost
    if not mm_cfg.webhook_secret and not mm_cfg.webhook_allow_unauthenticated:
        raise MattermostAuthError(
            "refusing to serve the chat webhook with no mattermost.webhook_secret: "
            "the endpoint would accept and act on any request. Set "
            "SECAGENT_MATTERMOST__WEBHOOK_SECRET to the token configured on the "
            "Mattermost slash command / outgoing webhook, or set "
            "mattermost.webhook_allow_unauthenticated=true if you really do mean an "
            "open endpoint (e.g. behind an authenticating proxy)."
        )
    app = FastAPI(title="secagent-chat", version="0.1.0")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "bot": settings.mattermost.bot_username}

    @app.post("/webhook")
    async def webhook(request: Request) -> dict[str, Any]:
        # Source-IP allow-list (defense-in-depth; pair with mTLS at the ingress) —
        # same shape as agents/review/webhook.
        allowed = settings.mattermost.webhook_allowed_ips
        if allowed:
            client_ip = request.client.host if request.client else ""
            if client_ip not in allowed:
                raise HTTPException(status_code=403, detail="source IP not allowed")

        # Mattermost slash commands and outgoing webhooks both default to
        # application/x-www-form-urlencoded; outgoing webhooks can optionally be
        # configured for JSON instead. Parsed by hand with the stdlib rather than
        # Starlette's `request.form()`, which needs the optional `python-multipart`
        # dependency this project does not carry (existing deps only) — and which
        # this project has never needed before, since the GitLab webhook only ever
        # reads JSON. Mattermost never sends multipart/form-data here, only simple
        # urlencoded bodies, so the stdlib parser is a complete, dependency-free fit.
        content_type = request.headers.get("content-type", "")
        raw = await request.body()
        if "application/json" in content_type:
            payload: dict[str, Any] = dict(json.loads(raw or b"{}"))
        else:
            payload = dict(parse_qsl(raw.decode("utf-8", errors="replace")))

        try:
            verify_token(settings, str(payload.get("token", "")))
        except MattermostAuthError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

        chat_request = ChatRequest(
            user_id=str(payload.get("user_id", "")),
            user_name=str(payload.get("user_name", "")),
            channel_id=str(payload.get("channel_id", "")),
            text=str(payload.get("text", "")),
            team=str(payload.get("team_domain", "") or payload.get("team_id", "")),
            post_id=str(payload.get("post_id", "")),
            trigger_id=str(payload.get("trigger_id", "")),
        )

        owns_mm = mattermost is None
        mm = mattermost or MattermostClient(settings.mattermost)
        try:
            try:
                result = dispatch_chat_event(settings, chat_request, mattermost=mm)
            except Exception as exc:  # never 500 to Mattermost; report and move on
                log.error("chat webhook dispatch failed: %s", exc)
                return {"action": "error", "error": str(exc)}
        finally:
            if owns_mm:
                mm.close()

        # No `text` field: the reply was already posted via the REST API above (as
        # the bot, in-thread). Echoing it here too would make Mattermost ALSO render
        # it as an immediate synchronous response — a double reply.
        return {"action": result.get("action"), "posted": result.get("posted")}

    return app


def serve(
    settings: Settings, host: str = "0.0.0.0", port: int = 8070, *,
    tls_certfile: str | None = None, tls_keyfile: str | None = None,
    tls_ca_certs: str | None = None,
) -> None:
    """Run the chat webhook receiver.

    Supplying ``tls_certfile``/``tls_keyfile`` serves over HTTPS; additionally
    supplying ``tls_ca_certs`` enables **mTLS** (CMMC-4) — identical to
    ``agents/review/webhook.serve``.
    """
    import ssl

    import uvicorn

    from ..netpolicy import enforce
    from ..security import enforce_fips_policy

    enforce_fips_policy(settings.fips.require_fips, settings.fips.allow_non_fips)
    enforce(settings)  # fail fast if endpoints violate the egress policy
    app = create_app(settings)
    ssl_kwargs: dict[str, Any] = {}
    if tls_certfile and tls_keyfile:
        ssl_kwargs["ssl_certfile"] = tls_certfile
        ssl_kwargs["ssl_keyfile"] = tls_keyfile
        if tls_ca_certs:
            ssl_kwargs["ssl_ca_certs"] = tls_ca_certs
            ssl_kwargs["ssl_cert_reqs"] = ssl.CERT_REQUIRED  # require client cert (mTLS)
    uvicorn.run(app, host=host, port=port, **ssl_kwargs)
