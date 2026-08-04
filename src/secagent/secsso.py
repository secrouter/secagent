"""OIDC token helpers for secagent's TWO distinct SecSSO identities.

**Service identity** (``fetch_token``/``get_token``): backs the ``secagent token`` CLI
command. Fetches a bearer token from SecSSO (the suite's OIDC identity provider) using
the standard client-credentials grant (RFC 6749 SS4.4), and caches it on disk until it
is near expiry. The cache is what makes it cheap to invoke on every request — pi
re-runs a ``models.json`` ``"!command"`` ``apiKey`` on every actual LLM call (see
``docs/models.md`` "Value Resolution"; not cached by pi itself), and secagent's own
``LLMConfig.api_key`` (via ``secretval.resolve_secret``) does the same when set to
``"!secagent token"`` — so without a cache here, every model call anywhere in the
suite would cost a SecSSO round trip.

The client secret is never read from config/YAML: ``SecSSOConfig.client_secret_env``
names an environment variable, and only that variable's value (read at request time)
is ever sent to SecSSO. The cached token is written with owner-only (0600) permissions,
matching the audit log / affordance store's at-rest posture (CMMC-2).

**Per-user identity** (``login``/``get_user_token``/``logout``): backs the ``secagent
login`` / ``secagent logout`` / ``secagent token --user`` CLI commands. Authenticates
the individual developer running the CLI via OIDC device authorization (RFC 8628)
against a PUBLIC client (no secret — see ``SecSSOConfig``'s docstring), and caches the
result at ``user_token_cache_path`` (also 0600) with the same auto-refresh-before-
expiry discipline as the service token. This is the ONE per-user token source the
onboarding feature promises: both secagent's own LLM calls
(``SECAGENT_LLM__API_KEY="!secagent token --user"``) and pi (``models.json``
``apiKey: "!secagent token --user"``) resolve through this exact cache — see
``docs/installation.md`` and ``onboarding.py``.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import httpx

from .config import SecSSOConfig
from .security import harden_path, secure_delete_file

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
            "https://secsso.<domain>:9000/application/o/token/"
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


# ════════════════════════════════════════════════════════════════════════════════════
# Per-user identity: OIDC device authorization (RFC 8628) — `secagent login` /
# `secagent logout` / `secagent token --user`. See the module docstring for how this
# differs from the service identity above.
# ════════════════════════════════════════════════════════════════════════════════════


@dataclass
class UserToken:
    """The developer's own OIDC token from SecSSO's device-authorization grant
    (``secagent login``). ``refresh_token`` may be empty if SecSSO did not issue one
    (IdP-dependent — RFC 6749 SS4.2 only says a token response "MAY include" one); a
    subsequent ``secagent login`` is then the only way to renew once ``access_token``
    expires. ``sub``/``email`` are best-effort DISPLAY identity, decoded (UNVERIFIED —
    cosmetic only, see ``_id_token_claims``) from the token response's OIDC
    ``id_token`` when SecSSO includes one; never used for an authorization decision —
    the access_token itself, verified by SecRouter, is what actually authenticates
    every request.
    """

    access_token: str
    refresh_token: str
    expires_at: float  # unix timestamp (time.time() semantics)
    sub: str = ""
    email: str = ""


@dataclass
class DeviceAuthorization:
    """RFC 8628 SS3.2 device authorization response — what ``secagent login`` shows
    the developer so they can approve the sign-in in a browser."""

    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_in: float
    interval: float


def _id_token_claims(id_token: str) -> dict[str, Any]:
    """Best-effort, UNVERIFIED decode of an OIDC ``id_token``'s payload claims.

    Cosmetic only — used solely so ``secagent login`` can print "logged in as ...".
    Never used for an authorization/trust decision: the ``access_token`` (verified by
    SecRouter itself on every request) is what actually authenticates, and nothing
    here checks this JWT's signature or expiry. A malformed/absent id_token just means
    the confirmation message omits identity; it is never treated as an error.
    """
    try:
        parts = id_token.split(".")
        if len(parts) != 3:
            return {}
        payload = parts[1]
        padded = payload + "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        return data if isinstance(data, dict) else {}
    except (ValueError, UnicodeDecodeError, TypeError):
        return {}


def _user_cache_path(config: SecSSOConfig) -> Path:
    return Path(config.user_token_cache_path).expanduser()


def _read_user_cache(path: Path) -> UserToken | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return None
    token = data.get("access_token")
    expires_at = data.get("expires_at")
    if not isinstance(token, str) or not token or not isinstance(expires_at, (int, float)):
        return None
    refresh = data.get("refresh_token")
    sub = data.get("sub")
    email = data.get("email")
    return UserToken(
        access_token=token,
        refresh_token=refresh if isinstance(refresh, str) else "",
        expires_at=float(expires_at),
        sub=sub if isinstance(sub, str) else "",
        email=email if isinstance(email, str) else "",
    )


def _write_user_cache(path: Path, token: UserToken) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        harden_path(path.parent, 0o700)
        # Atomic replace — see `_write_cache`'s comment; the same crash-safety reasoning
        # applies identically to this cache.
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps({
                "access_token": token.access_token,
                "refresh_token": token.refresh_token,
                "expires_at": token.expires_at,
                "sub": token.sub,
                "email": token.email,
            }),
            encoding="utf-8",
        )
        tmp.replace(path)
        harden_path(path, 0o600)
    except OSError as exc:
        # Caching is an optimization, not a correctness requirement — same discipline
        # as `_write_cache`.
        log.warning("secsso: could not write user token cache at %s: %s", path, exc)


def peek_user_token(config: SecSSOConfig) -> UserToken | None:
    """Read the cached per-user token as-is — no refresh, no network, ``None`` if
    absent/corrupt. Used by ``secagent doctor`` to report login status without side
    effects; ``get_user_token`` below is what actually resolves a *usable* token."""
    return _read_user_cache(_user_cache_path(config))


def request_device_code(
    config: SecSSOConfig, *, http: httpx.Client | None = None,
) -> DeviceAuthorization:
    """RFC 8628 SS3.1: request a device code + user code. Always hits the network."""
    if not config.device_authorization_url:
        raise SecSSOError(
            "secsso.device_authorization_url is not set — point it at your SecSSO "
            "OIDC device_authorization endpoint "
            "(SECAGENT_SECSSO__DEVICE_AUTHORIZATION_URL), e.g. "
            "https://secsso.<domain>:9000/application/o/device/ — normally set by "
            "`secagent init --domain <domain>`."
        )
    owns_http = http is None
    client = http or httpx.Client(timeout=30.0, verify=True)
    try:
        try:
            resp = client.post(
                config.device_authorization_url,
                data={"client_id": config.device_client_id, "scope": config.device_scope},
            )
        except httpx.HTTPError as exc:
            raise SecSSOError(
                f"SecSSO device authorization request to "
                f"{config.device_authorization_url} failed: {exc}"
            ) from exc
        if resp.status_code >= 400:
            raise SecSSOError(
                f"SecSSO device authorization request failed: "
                f"{resp.status_code} {resp.text[:200]}"
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise SecSSOError(
                f"SecSSO device authorization response was not JSON: {resp.text[:200]}"
            ) from exc
    finally:
        if owns_http:
            client.close()
    device_code, user_code = data.get("device_code"), data.get("user_code")
    verification_uri = data.get("verification_uri")
    if not device_code or not user_code or not verification_uri:
        raise SecSSOError(
            "SecSSO device authorization response missing device_code/user_code/"
            f"verification_uri: {json.dumps(data)[:200]}"
        )
    return DeviceAuthorization(
        device_code=device_code,
        user_code=user_code,
        verification_uri=verification_uri,
        verification_uri_complete=data.get("verification_uri_complete") or "",
        expires_in=float(data.get("expires_in", 600)),
        interval=float(data.get("interval", 5)),
    )


def _parse_token_response(data: dict[str, Any], now: float) -> UserToken:
    token = data.get("access_token")
    if not token:
        raise SecSSOError(f"SecSSO token response had no access_token: {json.dumps(data)[:200]}")
    expires_in = data.get("expires_in", 300)
    try:
        expires_in = float(expires_in)
    except (TypeError, ValueError):
        expires_in = 300.0
    id_token = data.get("id_token")
    claims = _id_token_claims(id_token) if id_token else {}
    sub, email = claims.get("sub", ""), claims.get("email", "")
    return UserToken(
        access_token=token,
        refresh_token=data.get("refresh_token") or "",
        expires_at=now + expires_in,
        sub=sub if isinstance(sub, str) else "",
        email=email if isinstance(email, str) else "",
    )


def _poll_device_token(
    config: SecSSOConfig,
    device: DeviceAuthorization,
    *,
    http: httpx.Client | None = None,
    now: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> UserToken:
    """RFC 8628 SS3.4/SS3.5: poll the token endpoint until the developer approves (or
    the device code expires or is denied).

    ``sleep``/``now`` are injectable for tests: a no-op ``sleep`` lets a test drive a
    "pending, pending, success" sequence with no real wait. Elapsed time is tracked by
    ACCUMULATING the polled ``interval`` rather than comparing against a wall-clock
    deadline, so the timeout path stays exact under a faked ``sleep`` too (it does not
    depend on ``time.time()`` actually advancing).
    """
    if not config.token_url:
        raise SecSSOError(
            "secsso.token_url is not set — point it at your SecSSO OIDC token "
            "endpoint (SECAGENT_SECSSO__TOKEN_URL)."
        )
    owns_http = http is None
    client = http or httpx.Client(timeout=30.0, verify=True)
    interval = max(1.0, device.interval)
    elapsed = 0.0
    try:
        while elapsed < device.expires_in:
            sleep(interval)
            elapsed += interval
            try:
                resp = client.post(
                    config.token_url,
                    data={
                        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                        "device_code": device.device_code,
                        "client_id": config.device_client_id,
                    },
                )
            except httpx.HTTPError as exc:
                raise SecSSOError(
                    f"SecSSO device token poll to {config.token_url} failed: {exc}"
                ) from exc
            try:
                data = resp.json()
            except ValueError as exc:
                raise SecSSOError(
                    f"SecSSO device token response was not JSON: {resp.text[:200]}"
                ) from exc
            if resp.status_code < 400 and data.get("access_token"):
                return _parse_token_response(data, time.time() if now is None else now)
            error = data.get("error", "")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval += 5.0  # RFC 8628 SS3.5: back off and keep polling
                continue
            # access_denied, expired_token, or anything else SecSSO returns: not
            # recoverable by continuing to poll.
            raise SecSSOError(
                f"SecSSO device login failed: {error or resp.status_code} "
                f"{data.get('error_description', '')}".strip()
            )
    finally:
        if owns_http:
            client.close()
    raise SecSSOError("SecSSO device code expired before login completed")


def login(
    config: SecSSOConfig,
    *,
    http: httpx.Client | None = None,
    now: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
    on_prompt: Callable[[DeviceAuthorization], None] | None = None,
) -> UserToken:
    """Run the full OIDC device-authorization flow (RFC 8628) and cache the result.

    Always hits the network and always re-authenticates — this is what makes
    ``secagent login`` idempotent-but-fresh (re-running it re-authenticates and
    overwrites the cache, rather than reusing whatever was cached before).
    ``on_prompt`` is called exactly once, right after SecSSO issues the device/user
    code, so the CALLER (the CLI) decides how to display the verification URL + code;
    this module itself never prints — matches ``fetch_token``/``get_token``'s
    discipline of staying print-free (presentation lives in ``cli.py``).
    """
    owns_http = http is None
    client = http or httpx.Client(timeout=30.0, verify=True)
    try:
        device = request_device_code(config, http=client)
        if on_prompt is not None:
            on_prompt(device)
        token = _poll_device_token(config, device, http=client, now=now, sleep=sleep)
    finally:
        if owns_http:
            client.close()
    _write_user_cache(_user_cache_path(config), token)
    return token


def refresh_user_token(
    config: SecSSOConfig,
    refresh_token: str,
    *,
    http: httpx.Client | None = None,
    now: float | None = None,
) -> UserToken:
    """RFC 6749 SS6: exchange a refresh token for a fresh access token. Always hits
    the network. The public device client authenticates with no secret here either."""
    if not config.token_url:
        raise SecSSOError(
            "secsso.token_url is not set — point it at your SecSSO OIDC token "
            "endpoint (SECAGENT_SECSSO__TOKEN_URL)."
        )
    owns_http = http is None
    client = http or httpx.Client(timeout=30.0, verify=True)
    try:
        try:
            resp = client.post(
                config.token_url,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": config.device_client_id,
                },
            )
        except httpx.HTTPError as exc:
            raise SecSSOError(
                f"SecSSO token refresh to {config.token_url} failed: {exc}"
            ) from exc
        try:
            data = resp.json()
        except ValueError as exc:
            raise SecSSOError(
                f"SecSSO token refresh response was not JSON: {resp.text[:200]}"
            ) from exc
    finally:
        if owns_http:
            client.close()
    if resp.status_code >= 400 or not data.get("access_token"):
        raise SecSSOError(
            f"SecSSO token refresh failed: {data.get('error', resp.status_code)} "
            f"{data.get('error_description', '')}".strip()
        )
    fresh = _parse_token_response(data, time.time() if now is None else now)
    # Some IdPs rotate the refresh token on every use, some don't — keep the old one if
    # SecSSO doesn't issue a new one, rather than dropping it and forcing a re-login on
    # the NEXT refresh (mirrors pi/extensions/secrouter-auth.ts's refreshToken(), the
    # reference implementation this is the CLI-side counterpart to).
    if not fresh.refresh_token:
        fresh = replace(fresh, refresh_token=refresh_token)
    return fresh


def get_user_token(
    config: SecSSOConfig, *, http: httpx.Client | None = None, now: float | None = None,
) -> str:
    """Return a valid per-user access token: the cache as-is when it has enough
    headroom, transparently refreshed when it doesn't (mirrors ``get_token``'s
    service-identity cache discipline exactly). Raises ``SecSSOError`` — with a
    message telling the caller to run `secagent login` — when there is no cache to
    work from at all, or when the cached token is past expiry with no refresh_token
    left to renew from, or when a refresh attempt itself fails.
    """
    now = time.time() if now is None else now
    path = _user_cache_path(config)
    cached = _read_user_cache(path)
    if cached is None:
        raise SecSSOError("no cached user token — run `secagent login` first")
    if cached.expires_at - now > config.expiry_buffer_s:
        return cached.access_token
    if not cached.refresh_token:
        raise SecSSOError(
            "cached user token has expired and there is no refresh token on file — "
            "run `secagent login` again"
        )
    try:
        fresh = refresh_user_token(config, cached.refresh_token, http=http, now=now)
    except SecSSOError as exc:
        raise SecSSOError(
            f"could not refresh the cached user token ({exc}) — run `secagent login` again"
        ) from exc
    _write_user_cache(path, fresh)
    return fresh.access_token


def logout(config: SecSSOConfig) -> bool:
    """Delete the cached per-user token (`secagent logout`), overwriting it first —
    matches ``security.secure_delete_file``'s at-rest discipline for a credential file
    (CMMC-2; the same helper ``affordance purge`` uses). Idempotent: returns whether a
    cache file actually existed to remove; never raises if there was none.
    """
    path = _user_cache_path(config)
    existed = path.exists()
    if existed:
        secure_delete_file(path)
    return existed
