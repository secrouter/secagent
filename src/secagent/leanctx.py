"""LeanCTX integration — the locked-down context-compression layer.

Single source of truth for turning :class:`secagent.config.LeanCtxConfig` into LeanCTX's own
on-disk config + process environment, ALWAYS with the suite's CMMC/air-gapped lockdown applied
(loopback-only, no update-check, hardened, telemetry off, prompt-cache-safe, and — unless
explicitly opted in — no persistent context store). Also the lazy, non-fatal bridge to the
local daemon used to compress secagent's own requests (see :func:`compress_messages`).

LeanCTX is OPTIONAL and must never break secagent: nothing here imports the SDK at module load,
and every daemon interaction degrades to a pass-through if LeanCTX is absent or unreachable.
See docs/leanctx.md.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .config import LeanCtxConfig

# LeanCTX reads its engine config from ``$XDG_CONFIG_HOME/lean-ctx/config.toml`` (default
# ``~/.config/lean-ctx/config.toml``) — verified against LeanCTX's own bench config.
DEFAULT_CONFIG_TOML = Path("~/.config/lean-ctx/config.toml")


def config_toml_path() -> Path:
    return DEFAULT_CONFIG_TOML.expanduser()


def endpoint_port(cfg: LeanCtxConfig, default: int = 4444) -> int:
    """The wire-compressor port from ``cfg.endpoint`` (LeanCTX's ``proxy_port``)."""
    return urlsplit(cfg.endpoint).port or default


def state_dir(cfg: LeanCtxConfig) -> Path:
    return Path(cfg.state_dir).expanduser()


def config_toml(cfg: LeanCtxConfig) -> str:
    """The locked-down LeanCTX engine config (``config.toml``).

    Keys are LeanCTX's own documented engine settings; the wire compressor is enabled with
    **cache-aware** history so SecRouter/SecLLM prompt caching keeps hitting (``rolling`` would
    rewrite a stable prefix every turn and turn cheap cache reads into full-price writes).
    ``secagent init`` writes this; re-init regenerates it.
    """
    lines = [
        "# secagent-managed LeanCTX config — locked down for the CMMC/air-gapped posture.",
        "# Written by `secagent init`; regenerated on re-init. See docs/leanctx.md.",
        'rules_injection = "off"',       # don't inject rule files into the agent context
        "minimal_overhead = true",
        'tool_profile = "minimal"',      # 6-tool core → smaller injected prefix
        "structure_first = true",        # structure-first cold reads
        "proxy_enabled = true",          # the wire compressor (agent/secagent → SecRouter)
        f"proxy_port = {endpoint_port(cfg)}",
        "",
        "[proxy]",
        f'history_mode = "{cfg.proxy_history_mode}"',   # keep the SecRouter prompt cache hitting
    ]
    if not cfg.persist_context:
        # Best-effort in-config declaration; the primary no-persist lever is the env below +
        # not enabling the MCP memory tools (see lockdown_env / docs/leanctx.md).
        lines += ["", "[memory]", "enabled = false"]
    return "\n".join(lines) + "\n"


def lockdown_env(cfg: LeanCtxConfig) -> dict[str, str]:
    """The ``LEAN_CTX_*`` environment handed to every LeanCTX process (daemon, pi extension,
    CLI), enforcing the lockdown REGARDLESS of any hand-edited ``config.toml`` — so the
    air-gapped/CMMC invariants can't be silently dropped. ``secagent doctor`` verifies a running
    process actually carries these.

    Verified keys: ``NO_UPDATE_CHECK``, ``HARDEN``, ``PI_MODE``, ``PI_ENABLE_MCP``,
    ``PROXY_HISTORY_MODE``. The telemetry/persist keys are belt-and-suspenders — telemetry is
    opt-in upstream (the suite simply never enables it), and the primary no-persist guarantee is
    leaving the MCP memory tools off (``pi_enable_mcp = false``). See docs/leanctx.md.
    """
    env: dict[str, str] = {
        "LEAN_CTX_PI_MODE": cfg.pi_mode,
        "LEAN_CTX_PI_ENABLE_MCP": "1" if cfg.pi_enable_mcp else "0",
        "LEAN_CTX_PROXY_HISTORY_MODE": cfg.proxy_history_mode,
        # Telemetry stays off (opt-in upstream; asserted so it can never flip on).
        "LEAN_CTX_TELEMETRY": "0",
        "LEAN_CTX_NO_TELEMETRY": "1",
    }
    if cfg.no_update_check:
        env["LEAN_CTX_NO_UPDATE_CHECK"] = "1"    # air-gapped: no update phone-home
    if cfg.harden:
        env["LEAN_CTX_HARDEN"] = "1"
    if not cfg.persist_context:
        env["LEAN_CTX_NO_PERSIST"] = "1"
        env["LEAN_CTX_EPHEMERAL"] = "1"
    return env


def binary_installed() -> bool:
    """Whether the ``lean-ctx`` binary is on PATH (the daemon/CLI)."""
    return shutil.which("lean-ctx") is not None


def sdk_available() -> bool:
    """Whether the ``lean-ctx-client`` SDK can be imported (lazy — never at module load, so
    secagent has no hard dependency on LeanCTX being installed)."""
    try:
        import leanctx  # noqa: F401  (the lean-ctx-client package)
    except Exception:  # noqa: BLE001 — any import problem = "not available", never fatal
        return False
    return True


def compress_messages(cfg: LeanCtxConfig, messages: list[dict[str, Any]],
                      *, model: str) -> list[dict[str, Any]]:
    """Compress an OpenAI-style ``messages`` list via the local LeanCTX daemon before secagent
    posts it to SecRouter (UC100/UC101). NON-FATAL by contract: if LeanCTX is disabled, its SDK
    isn't installed, or the daemon is unreachable/errors, the ORIGINAL messages are returned
    unchanged — a compression outage must never drop or corrupt a governed request.

    Only used when ``cfg.enabled and cfg.compress_own_calls``; the caller checks that, but this
    re-checks so it's safe to call unconditionally.
    """
    if not (cfg.enabled and cfg.compress_own_calls) or not messages:
        return messages
    try:
        from leanctx import LeanCtxClient  # lazy: no hard dep

        client = LeanCtxClient(base_url=cfg.endpoint.rstrip("/"))
        compressed = client.compress(messages=messages, model=model)
        # Defensive: only accept a well-formed non-empty result; otherwise pass through.
        if isinstance(compressed, list) and compressed:
            return compressed
    except Exception:  # noqa: BLE001 — daemon down / API drift / anything: pass through
        pass
    return messages
