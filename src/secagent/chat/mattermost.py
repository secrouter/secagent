"""Mattermost REST v4 client — the outbound half of UC101.

A small, FIPS-friendly (system-OpenSSL TLS, no extra crypto) wrapper, mirroring
``mcp/gitlab_harness.GitLabClient``'s shape: the bot posts replies as itself using a
bot/personal access token. The inbound half (receiving mentions/slash-commands) is
``webhook.py``.
"""

from __future__ import annotations

from typing import Any

import httpx

from ..config import MattermostConfig


class MattermostError(RuntimeError):
    pass


class MattermostClient:
    def __init__(self, config: MattermostConfig, http: httpx.Client | None = None) -> None:
        self.config = config
        self._owns_http = http is None
        # Trailing slash on base_url + relative request paths so httpx preserves the
        # "/api/v4" prefix (a leading-slash path would otherwise replace it) — same
        # convention as GitLabClient.
        self._http = http or httpx.Client(
            base_url=config.url.rstrip("/") + "/api/v4/",
            headers={"Authorization": f"Bearer {config.bot_token}"},
            timeout=30.0,
            verify=config.verify_tls,  # system OpenSSL / FIPS trust store
        )

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> MattermostClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def create_post(self, channel_id: str, message: str, root_id: str = "") -> dict[str, Any]:
        """Post ``message`` to ``channel_id`` as the bot. ``root_id`` (the triggering
        post's id, when known) threads the reply; omitted, it starts a new thread —
        the right default for a slash command, which has no prior post to root on."""
        body: dict[str, Any] = {"channel_id": channel_id, "message": message}
        if root_id:
            body["root_id"] = root_id
        resp = self._http.post("posts", json=body)
        if resp.status_code >= 400:
            raise MattermostError(
                f"POST posts -> {resp.status_code}: {resp.text[:200]}"
            )
        return resp.json()
