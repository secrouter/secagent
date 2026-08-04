"""OIDC ``client_credentials`` token helper for secagent's own SecSSO service identity.

Backs the ``secagent token`` CLI command: fetches a bearer token from SecSSO (the
suite's OIDC identity provider) using the standard client-credentials grant (RFC 6749
SS4.4), and caches it on disk until it is near expiry. The cache is what makes it cheap
to invoke on every request — pi re-runs a ``models.json`` ``"!command"`` ``apiKey`` on
every actual LLM call (see ``docs/models.md`` "Value Resolution"; not cached by pi
itself), and secagent's own ``LLMConfig.api_key`` (via ``secretval.resolve_secret``)
does the same when set to ``"!secagent token"`` — so without a cache here, every model
call anywhere in the suite would cost a SecSSO round trip.

The client secret is never read from config/YAML: ``SecSSOConfig.client_secret_env``
names an environment variable, and only that variable's value (read at request time)
is ever sent to SecSSO. The cached token is written with owner-only (0600) permissions,
matching the audit log / affordance store's at-rest posture (CMMC-2).
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import SecSSOConfig
from .security import harden_path

log = logging.getLogger(__name__)


class SecSSOError(RuntimeError):
    """SecSSO is not configured, or the token request/response was unusable."""


@dataclass
class CachedToken:
    access_token: str
    expires_at: float  # unix timestamp (time.time() semantics)


def _cache_path(config: SecSSOConfig) -> Path:
    return Path(config.token_cache_path).expanduser()


def _read_cache(path: Path) -> CachedToken | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return None
    token = data.get("access_token")
    expires_at = data.get("expires_at")
    if not isinstance(token, str) or not token or not isinstance(expires_at, (int, float)):
        return None
    return CachedToken(access_token=token, expires_at=float(expires_at))


def _write_cache(path: Path, token: CachedToken) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        harden_path(path.parent, 0o700)
        # Atomic replace: a crash mid-write must not leave a truncated/corrupt cache
        # that a later read silently treats as "no cached token" (harmless) or, worse,
        # a partially-written valid-looking JSON blob (not harmless).
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps({"access_token": token.access_token, "expires_at": token.expires_at}),
            encoding="utf-8",
        )
        tmp.replace(path)
        harden_path(path, 0o600)
    except OSError as exc:
        # Caching is an optimization, not a correctness requirement: the fetched
        # token is still returned to the caller even if it could not be persisted.
        log.warning("secsso: could not write token cache at %s: %s", path, exc)


def _client_secret(config: SecSSOConfig) -> str:
    if not config.client_secret_env:
        raise SecSSOError(
            "secsso.client_secret_env is not set — point it at the name of the "
            "environment variable holding the SecSSO client secret (default "
            "SECAGENT_CLIENT_SECRET), and set that variable out of band."
        )
    secret = os.environ.get(config.client_secret_env, "")
    if not secret:
        raise SecSSOError(
            f"environment variable {config.client_secret_env} (secsso.client_secret_env) "
            "is unset or empty — the SecSSO client secret is required to fetch a token."
        )
    return secret


def fetch_token(
    config: SecSSOConfig, *, http: httpx.Client | None = None, now: float | None = None,
) -> CachedToken:
    """Perform the OIDC client_credentials grant against SecSSO. Always hits the network.

    ``now`` is injectable for tests (see ``get_token``); otherwise ``time.time()``.

    ``config.username`` is NOT sent — RFC 6749's client_credentials grant carries no
    resource-owner identity, only ``client_id``/``client_secret``/``scope``. It is
    metadata describing the service account SecSSO is expected to resolve the client
    to (e.g. a Keycloak service-account username), surfaced in error messages, not a
    request parameter — sending it would silently diverge from the standard grant.
    """
    if not config.token_url:
        raise SecSSOError(
            "secsso.token_url is not set — point it at your SecSSO OIDC token endpoint "
            "(SECAGENT_SECSSO__TOKEN_URL), e.g. "
            "https://secsso.<domain>/realms/secrouter/protocol/openid-connect/token"
        )
    secret = _client_secret(config)
    owns_http = http is None
    client = http or httpx.Client(timeout=30.0, verify=True)  # system OpenSSL / FIPS trust store
    try:
        try:
            resp = client.post(
                config.token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": config.client_id,
                    "client_secret": secret,
                    "scope": config.scope,
                },
            )
        except httpx.HTTPError as exc:
            raise SecSSOError(
                f"SecSSO token request to {config.token_url} failed: {exc}"
            ) from exc
        if resp.status_code >= 400:
            raise SecSSOError(
                f"SecSSO token request failed: {resp.status_code} {resp.text[:200]} "
                f"(client_id={config.client_id!r}, identity={config.username!r})"
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise SecSSOError(f"SecSSO token response was not JSON: {resp.text[:200]}") from exc
    finally:
        if owns_http:
            client.close()
    token = data.get("access_token")
    if not token:
        raise SecSSOError(f"SecSSO token response had no access_token: {json.dumps(data)[:200]}")
    expires_in = data.get("expires_in", 300)
    try:
        expires_in = float(expires_in)
    except (TypeError, ValueError):
        expires_in = 300.0
    start = time.time() if now is None else now
    return CachedToken(access_token=token, expires_at=start + expires_in)


def get_token(
    config: SecSSOConfig, *, http: httpx.Client | None = None, now: float | None = None,
) -> str:
    """Return a valid access token, serving the file cache when it has enough headroom.

    ``now`` is injectable for tests; otherwise ``time.time()``. A cached token is used
    as-is when it has more than ``config.expiry_buffer_s`` seconds left, so a token
    already in flight to SecRouter does not expire mid-request. Concurrent invocations
    (e.g. several parallel LLM calls each spawning their own ``secagent token``) are not
    lock-coordinated — a race can cause a few redundant fetches under heavy concurrency,
    but the atomic cache write means it never corrupts, so this is a throughput cost,
    not a correctness one.
    """
    now = time.time() if now is None else now
    path = _cache_path(config)
    cached = _read_cache(path)
    if cached is not None and cached.expires_at - now > config.expiry_buffer_s:
        return cached.access_token
    fresh = fetch_token(config, http=http, now=now)
    _write_cache(path, fresh)
    return fresh.access_token
