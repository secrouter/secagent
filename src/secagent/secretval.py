"""Shared secret-value resolution: literal / env-var interpolation / shell command.

Mirrors pi's own ``models.json`` ``apiKey``/``headers`` "Value Resolution" syntax
(https://pi.dev docs, ``docs/models.md``) so operators learn one convention across the
whole suite: secagent's own ``llm.api_key`` supports it too, e.g.
``SECAGENT_LLM__API_KEY="!secagent token"`` reuses the exact same SecSSO service-token
helper (see ``secsso.py``) that a pi ``models.json`` provider would invoke via
``apiKey: "!secagent token"``.

Resolution happens at the point of use (once per LLM request — see
``llm/client.py``), not once at config load, so a refreshing ``!command`` value is
picked up on every call rather than baked in for the process's lifetime. This matches
pi's documented behavior for ``models.json`` (not ``auth.json``, which pi caches for
the process lifetime): "shell commands are resolved at request time... pi
intentionally does not apply built-in TTL, stale reuse, or recovery logic for
arbitrary commands" — the same is true here; a slow or rate-limited command should
implement its own caching (``secagent token`` does, via a file cache).
"""

from __future__ import annotations

import os
import re
import subprocess

# Matches "$VAR" or "${VAR}". `$$`/`$!` escapes are handled separately (as literal
# substrings) before this ever runs, so they can never be mistaken for it.
_ENV_REF = re.compile(r"\$\{(\w+)\}|\$(\w+)")

# Sentinels used to protect `$$`/`$!` escapes from the env-var regex. Control
# characters are never legal in these config values, so collision is not a concern.
_DOLLAR_SENTINEL = "\0SECAGENT_DOLLAR\0"
_BANG_SENTINEL = "\0SECAGENT_BANG\0"


class SecretResolutionError(RuntimeError):
    """A ``!command`` failed, or a referenced environment variable is unset."""


def resolve_secret(value: str, *, timeout: float = 15.0) -> str:
    """Resolve one config value using pi-compatible syntax.

    - ``"!command"``       -- execute the rest via the shell, return stdout (stripped
      of surrounding whitespace). Non-zero exit or a spawn failure raises.
    - ``"$VAR"``/``"${VAR}"`` -- environment interpolation; works inside a larger
      literal (e.g. ``"${PREFIX}_${SUFFIX}"``). A missing variable raises rather than
      silently resolving to an empty string.
    - ``"$$"`` / ``"$!"``  -- escapes for a literal ``$`` / ``!`` (so a value can
      start with a literal ``!`` without triggering command execution).
    - anything else        -- returned unchanged (the common case: a plain literal
      like ``"not-needed"`` costs one no-op regex pass).
    """
    if not value:
        return value
    if value.startswith("!"):
        return _run_command(value[1:], timeout=timeout)
    return _interpolate_env(value)


def _run_command(command: str, *, timeout: float) -> str:
    try:
        result = subprocess.run(  # noqa: S602 - operator-configured command, by design
            command, shell=True, capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SecretResolutionError(f"command {command!r} failed to run: {exc}") from exc
    if result.returncode != 0:
        raise SecretResolutionError(
            f"command {command!r} exited {result.returncode}: {result.stderr.strip()[:200]}"
        )
    return result.stdout.strip()


def _interpolate_env(value: str) -> str:
    # Protect escapes first (as literal substrings) so the regex below never sees a
    # "$$" or "$!" sequence and cannot misinterpret it as the start of a $VAR ref.
    value = value.replace("$$", _DOLLAR_SENTINEL).replace("$!", _BANG_SENTINEL)

    def repl(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        if name not in os.environ:
            raise SecretResolutionError(f"environment variable {name} is not set")
        return os.environ[name]

    value = _ENV_REF.sub(repl, value)
    return value.replace(_DOLLAR_SENTINEL, "$").replace(_BANG_SENTINEL, "!")
