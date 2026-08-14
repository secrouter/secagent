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

import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .config import LeanCtxConfig

# LeanCTX reads its engine config from ``$XDG_CONFIG_HOME/lean-ctx/config.toml`` (default
# ``~/.config/lean-ctx/config.toml``) — verified against LeanCTX's own bench config.
DEFAULT_CONFIG_TOML = Path("~/.config/lean-ctx/config.toml")

# The pi-lean-ctx extension entry point, loaded into a pi process with ``pi -e <path>`` at LAUNCH
# time (see :func:`pi_launch_args`) — NOT auto-discovered from pi's global settings.json. This is
# the whole point of the launch-time model: LeanCTX rides along only on the pi processes secagent
# (or SecChat's runner) actually starts, never on a bare host ``pi`` and never on the operator's
# other agents (claude/codex/…). ``lean-ctx init --agent pi`` drops the package here as a sibling
# of pi's own node_modules so its peer imports resolve. Override with SECAGENT_PI_LEANCTX_EXTENSION
# (e.g. the path baked into SecChat's container image).
PI_EXTENSION_ENV = "SECAGENT_PI_LEANCTX_EXTENSION"
DEFAULT_PI_EXTENSION = Path("~/.pi/agent/npm/node_modules/pi-lean-ctx/extensions/index.ts")
# pi's global extension registry — ``lean-ctx init --agent pi`` adds ``npm:pi-lean-ctx`` here so
# EVERY pi run auto-loads it. The launch-time model removes that entry (see
# :func:`_deregister_global_pi_extension`) so the extension loads ONLY via the explicit ``-e`` a
# secagent launch passes — never on a bare host pi.
PI_SETTINGS_JSON = Path("~/.pi/agent/settings.json")
PI_EXTENSION_PACKAGE = "npm:pi-lean-ctx"
# pi's global rule drop (``~/.pi/rules/lean-ctx.md``) + the per-project ``AGENTS.md`` /
# ``LEAN-CTX.md`` a wrap leaves in the cwd — operator-visible artifacts the launch-time model does
# not want. Removed best-effort during install.
PI_GLOBAL_RULE = Path("~/.pi/rules/lean-ctx.md")


def config_toml_path() -> Path:
    return DEFAULT_CONFIG_TOML.expanduser()


def pi_extension_entry(env: dict[str, str] | None = None) -> Path | None:
    """The pi-lean-ctx extension entry to load with ``pi -e`` at launch, or ``None`` when it isn't
    installed. Honours ``$SECAGENT_PI_LEANCTX_EXTENSION`` (the container bakes its own path) before
    the default under pi's npm tree. Existence-checked so a caller can cleanly skip the ``-e`` when
    the package was never installed (LeanCTX is optional — a missing extension is never fatal)."""
    src = env if env is not None else os.environ
    override = src.get(PI_EXTENSION_ENV)
    candidate = Path(override).expanduser() if override else DEFAULT_PI_EXTENSION.expanduser()
    return candidate if candidate.exists() else None


def pi_launch_args(cfg: LeanCtxConfig, *, env: dict[str, str] | None = None) -> list[str]:
    """The pi CLI args that attach LeanCTX to ONE launch: ``["-e", <extension>]`` when LeanCTX is
    enabled and the extension is installed, else ``[]``. ``-e`` loads regardless of pi's
    ``--no-extensions`` (it's an explicitly-named extension, not folder auto-discovery), so this is
    the sanctioned way SecChat's gated runner adds LeanCTX alongside its own extensions. Pair with
    :func:`lockdown_env` on the same process."""
    if not cfg.enabled:
        return []
    entry = pi_extension_entry(env)
    return ["-e", str(entry)] if entry is not None else []


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


def write_config(cfg: LeanCtxConfig, path: Path | None = None) -> Path:
    """Write the locked-down :func:`config_toml` to ``path`` (default LeanCTX's config
    location), ``0600``, creating parents. Returns the path. Pure file I/O — the pi-extension
    install (``lean-ctx init --agent pi``) is separate + best-effort (see
    :func:`install_pi_extension`)."""
    target = path or config_toml_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(config_toml(cfg))
    target.chmod(0o600)
    return target


def _deregister_global_pi_extension(settings_path: Path | None = None,
                                    rule_path: Path | None = None) -> list[str]:
    """Undo the GLOBAL auto-load that ``lean-ctx init --agent pi`` leaves behind: drop
    ``npm:pi-lean-ctx`` from pi's ``settings.json`` ``packages`` and remove the global rule drop.

    The package files stay installed (that's the benign npm cache the ``-e`` launch points at) —
    only the always-on registration is removed, so a bare host ``pi`` (or the operator's other
    agents) never load LeanCTX. It attaches ONLY to the pi processes a secagent launch starts (via
    :func:`pi_launch_args`). Idempotent + best-effort: a missing or malformed file is not an
    error."""
    import json

    steps: list[str] = []
    settings = (settings_path or PI_SETTINGS_JSON).expanduser()
    try:
        if settings.exists():
            data = json.loads(settings.read_text() or "{}")
            packages = data.get("packages")
            if isinstance(packages, list) and PI_EXTENSION_PACKAGE in packages:
                data["packages"] = [p for p in packages if p != PI_EXTENSION_PACKAGE]
                settings.write_text(json.dumps(data, indent=2) + "\n")
                steps.append("de-registered pi-lean-ctx from pi's global settings.json "
                             "(loads only via secagent's launch -e now)")
    except Exception as exc:  # noqa: BLE001 — best-effort cleanup, never fatal
        steps.append(f"could not de-register global pi extension — skipped ({exc})")
    rule = (rule_path or PI_GLOBAL_RULE).expanduser()
    try:
        if rule.exists():
            rule.unlink()
            steps.append("removed the global pi lean-ctx rule drop (~/.pi/rules/lean-ctx.md)")
    except Exception as exc:  # noqa: BLE001
        steps.append(f"could not remove global pi rule — skipped ({exc})")
    return steps


def install_pi_extension(cfg: LeanCtxConfig, *,
                         runner: Callable[..., Any] | None = None) -> list[str]:
    """Install the pi-lean-ctx extension for LAUNCH-TIME use and scope it to secagent-started pi.

    Runs ``lean-ctx init --agent pi`` ONCE to drop the package + its deps into pi's npm tree (so the
    ``-e`` launch can resolve it), then immediately :func:`_deregister_global_pi_extension` so it is
    NOT auto-loaded by every pi. Deliberately does NOT run ``lean-ctx harden`` and does NOT wrap the
    operator's shell/Claude Code — LeanCTX must ride along only on the pi processes secagent (or
    SecChat's runner) launches, per the "LeanCTX only for pi-with-secagent" rule. The per-process
    lockdown still applies via :func:`lockdown_env` (incl. ``LEAN_CTX_HARDEN=1`` when
    ``cfg.harden``) at launch, not as a global mutation here.

    NEVER fails onboarding: a missing ``lean-ctx`` binary (or a failed step) is reported and skipped
    — the ``config.toml`` is still written and ``secagent doctor`` flags the gap. ``runner``
    (``(argv, env) -> completed``) is injectable for tests.
    """
    def _default_runner(argv: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
        return subprocess.run(argv, env=env, capture_output=True, text=True,
                              timeout=120, check=False)

    run = runner or _default_runner
    if not binary_installed():
        return ["lean-ctx not found — skipped pi extension install (install it, then re-run "
                "`secagent init`); see docs/leanctx.md"]
    env = {**os.environ, **lockdown_env(cfg)}
    init_argv = ["lean-ctx", "init", "--agent", "pi"]
    if cfg.pi_enable_mcp:
        init_argv += ["--mode", "mcp"]
    steps: list[str] = []
    try:
        result = run(init_argv, env)
        rc = getattr(result, "returncode", 0)
        label = "installed the pi-lean-ctx extension (lean-ctx init --agent pi)"
        steps.append(label if rc == 0 else f"{label} — WARN (exit {rc})")
    except Exception as exc:  # noqa: BLE001 — best-effort; a failed step never aborts init
        steps.append(f"installing the pi-lean-ctx extension — skipped ({exc})")
        return steps
    # Scope it to secagent-launched pi only — never a global auto-load, never an operator harden.
    steps += _deregister_global_pi_extension()
    return steps


# Back-compat alias for the pre-launch-time name; new code calls install_pi_extension.
wire_pi = install_pi_extension


def launch_pi(cfg: LeanCtxConfig, pi_args: list[str], *, pi_bin: str = "pi",
              extra_env: dict[str, str] | None = None,
              exec_fn: Callable[[str, list[str], dict[str, str]], Any] | None = None) -> list[str]:
    """Build (and, by default, ``exec``) a pi command with LeanCTX attached FOR THIS PROCESS: the
    ``-e <extension>`` from :func:`pi_launch_args` plus the :func:`lockdown_env`, merged over the
    caller's ``extra_env``. This is the one place "secagent launches pi with LeanCTX configured"
    lives; SecChat's runner mirrors the same contract in TypeScript.

    Returns the argv it built. ``exec_fn(pi_bin, argv, env)`` is injectable (tests pass a capturing
    stub); the default replaces the current process with pi via ``os.execvpe`` so signals/exit codes
    pass straight through. When LeanCTX is disabled or uninstalled, pi still launches — just without
    the ``-e`` (a clean no-LeanCTX fallback)."""
    argv = [pi_bin, *pi_launch_args(cfg), *pi_args]
    env = {**os.environ, **(extra_env or {})}
    if cfg.enabled:
        env.update(lockdown_env(cfg))
    if exec_fn is not None:
        exec_fn(pi_bin, argv, env)
    else:
        os.execvpe(pi_bin, argv, env)
    return argv


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
    posts it to SecRouter (UC100). NON-FATAL by contract: if LeanCTX is disabled, its SDK
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
