"""``secagent doctor`` — runtime self-checks for FIPS posture and dependencies.

This is both an operational health check and a lightweight FIPS conformance gate:
it verifies the crypto backend, scans the running package for forbidden hash usage,
and probes the configured LLM endpoint and the Draw.io toolchain.
"""

from __future__ import annotations

import contextlib
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import Settings
from .secretval import SecretResolutionError, resolve_secret
from .security import (
    FORBIDDEN_HASHES,
    harden_path,
    openssl_fips_enabled,
    openssl_version,
)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    # "warn" means non-fatal: surfaced but does not fail the overall run.
    severity: str = "error"  # "error" | "warn" | "info"


# Matches hashlib calls to forbidden algorithms that are NOT explicitly flagged as
# non-security use (i.e. missing usedforsecurity=False).
_HASH_CALL_RE = re.compile(
    r"hashlib\.(" + "|".join(FORBIDDEN_HASHES) + r")\s*\(",
)
_USEDFORSECURITY_FALSE_RE = re.compile(r"usedforsecurity\s*=\s*False")


def _scan_forbidden_hashes(package_root: Path) -> list[str]:
    """Return ``path:line`` strings where forbidden hashes appear in security use."""
    findings: list[str] = []
    for py in package_root.rglob("*.py"):
        # The security module names the algorithms in a constant + a probe; skip it.
        if py.name in {"security.py", "doctor.py"}:
            continue
        try:
            text = py.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            if _HASH_CALL_RE.search(line) and not _USEDFORSECURITY_FALSE_RE.search(line):
                findings.append(f"{py}:{i}")
    return findings


def check_openssl() -> Check:
    return Check("openssl", True, openssl_version(), severity="info")


def check_fips(require: bool, allow_non_fips: bool = False) -> Check:
    enabled = openssl_fips_enabled()
    if enabled:
        return Check("fips_mode", True, "OpenSSL is enforcing FIPS mode")
    if require and allow_non_fips:
        # FIPS expected but explicitly permitted to run on a non-FIPS host.
        return Check(
            "fips_mode", True,
            "OpenSSL is NOT in FIPS mode; running anyway (fips.allow_non_fips=true)",
            severity="warn",
        )
    detail = "OpenSSL is NOT in FIPS mode (non-approved algorithms are permitted)"
    return Check("fips_mode", not require, detail, severity="error" if require else "warn")


def check_no_weak_hashes() -> Check:
    root = Path(__file__).resolve().parent
    findings = _scan_forbidden_hashes(root)
    if findings:
        return Check(
            "weak_hash_scan",
            False,
            "forbidden hash usage: " + ", ".join(findings[:10]),
        )
    return Check("weak_hash_scan", True, "no forbidden hash usage in secagent package")


_CHROMIUM_NAMES = ("chromium", "chromium-browser", "chromium-headless-shell",
                   "headless-shell", "google-chrome", "chrome")


def check_diagram_renderer(settings: Settings) -> Check:
    """Report readiness of the configured diagram backend (diagrams.renderer)."""
    renderer = settings.diagrams.renderer
    if renderer == "svg":
        return Check("diagrams", True,
                     "svg backend (built-in renderer; no external binary needed)",
                     severity="info")
    if renderer == "chromium":
        chrome = settings.diagrams.chromium_binary or next(
            (b for b in _CHROMIUM_NAMES if shutil.which(b)), None)
        viewer = Path(os.environ.get(
            "SECAGENT_DRAWIO_VIEWER_JS", "/usr/share/secagent/drawio-viewer.min.js"))
        if chrome and viewer.exists():
            return Check("diagrams", True, f"chromium backend: {chrome} (+ draw.io viewer)")
        return Check("diagrams", True,
                     "chromium backend selected but chromium/viewer missing; "
                     "diagrams fall back to svg", severity="warn")
    if renderer == "drawio":
        binary = shutil.which("drawio") or shutil.which("drawio-desktop")
        if binary and shutil.which("xvfb-run"):
            return Check("diagrams", True, f"drawio backend: {binary} (+ xvfb-run)")
        return Check("diagrams", True,
                     "drawio backend selected but drawio/xvfb-run missing; "
                     "diagrams fall back to svg", severity="warn")
    return Check("diagrams", True,
                 f"unknown renderer '{renderer}'; diagrams fall back to svg",
                 severity="warn")


def check_ikos() -> Check:
    binary = shutil.which("ikos")
    if binary:
        return Check("ikos", True, f"found {binary}", severity="info")
    return Check(
        "ikos", True,
        "ikos not found; UC3 runs in ingest mode only (analyze ingest)",
        severity="info",
    )


def check_docs_extra() -> Check:
    have = importlib.util.find_spec("sphinx") is not None
    return Check(
        "docs_extra",
        True,
        "sphinx installed" if have else "sphinx not installed (pip install 'secagent[docs]')",
        severity="info" if have else "warn",
    )


# ── Developer onboarding (install.sh / secagent init / secagent login) ─────────────
#
# The checks below report the state of the ONE-COMMAND-INSTALL + TWO-COMMAND-SETUP
# flow (see docs/installation.md): is the runtime new enough, is pi/Node present (both
# optional — pi is a separate, optional agent runtime), has `secagent init` run, and
# is the developer actually logged in. None of these are FIPS/security gates, so
# (aside from the Python floor) they warn rather than fail.


def check_python_version() -> Check:
    v = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    # Suppressing ruff's UP036 deliberately: it assumes this comparison is dead code
    # because pyproject.toml already declares requires-python = ">=3.11" -- but that
    # metadata is exactly what this RUNTIME check exists to verify actually held (a
    # stale venv, a misconfigured PATH, or `install.sh` picking the wrong interpreter
    # can all still run secagent under something older).
    if sys.version_info >= (3, 11):  # noqa: UP036
        return Check("python_version", True, f"Python {v}", severity="info")
    return Check("python_version", False,
                 f"Python {v} — secagent requires >=3.11 (see install.sh / "
                 "docs/installation.md)")


def check_secagent_version() -> Check:
    from . import __version__

    return Check("secagent_version", True, f"secagent {__version__}", severity="info")


def _binary_version(binary: str, flag: str) -> str:
    """Best-effort ``<binary> <flag>`` output, first non-empty line. Empty string on
    any failure — never raises, since this is purely cosmetic (doctor still reports
    the binary as *present* from `shutil.which` alone)."""
    try:
        result = subprocess.run(  # noqa: S603 - fixed binary from shutil.which, fixed flag
            [binary, flag], capture_output=True, text=True, timeout=5.0, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    for line in (result.stdout or result.stderr or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def check_pi_runtime() -> Check:
    """pi (pi.dev) is OPTIONAL for secagent's own CLI — only `secagent init` wires it
    up, for a developer who wants it as their agent runtime. A missing `pi` never
    fails doctor, only warns; see docs/installation.md."""
    binary = shutil.which("pi")
    if not binary:
        return Check("pi_runtime", True,
                     "pi not found on PATH (optional — see docs/installation.md)",
                     severity="warn")
    version = _binary_version(binary, "--version")
    detail = f"found {binary}" + (f" ({version})" if version else "")
    return Check("pi_runtime", True, detail, severity="info")


def check_node_runtime() -> Check:
    """Node/npm are needed only to install/run pi (optional) — see check_pi_runtime."""
    node, npm = shutil.which("node"), shutil.which("npm")
    if not node or not npm:
        missing = ", ".join(name for name, path in (("node", node), ("npm", npm)) if not path)
        return Check("node_runtime", True,
                     f"missing: {missing} (needed only to install/run pi, which is optional)",
                     severity="warn")
    return Check("node_runtime", True, f"node: {node}, npm: {npm}", severity="info")


def check_onboarding() -> Check:
    """Whether `secagent init` has run. Both files it writes are fixed, well-known
    per-user paths (not settings-driven — see `onboarding.py`), so this checks those
    exact locations regardless of the active config."""
    from .onboarding import default_models_json_path, default_user_config_path

    models_json = default_models_json_path()
    user_config = default_user_config_path()
    missing = [str(p) for p in (models_json, user_config) if not p.exists()]
    if missing:
        return Check("onboarding", True,
                     "not done — missing " + ", ".join(missing) +
                     " (run `secagent init --domain <domain>`)", severity="warn")
    return Check("onboarding", True, f"done: {models_json}, {user_config}", severity="info")


def check_user_login(settings: Settings) -> Check:
    """Whether `secagent login` has a live cached token — no network, no refresh
    attempt (that is `get_user_token`'s job, run lazily on actual use); this just
    reports what is already on disk."""
    from .secsso import peek_user_token

    cached = peek_user_token(settings.secsso)
    if cached is None:
        return Check("user_login", True,
                     "not logged in (run `secagent login`)", severity="warn")
    remaining = cached.expires_at - time.time()
    if remaining <= 0:
        return Check("user_login", True,
                     "user token cached but EXPIRED (run `secagent login` again)",
                     severity="warn")
    who = cached.sub or cached.email or "developer"
    return Check("user_login", True,
                 f"logged in ({who}); token valid for {remaining / 3600:.1f}h more",
                 severity="info")


def fix_permissions(settings: Settings) -> list[Path]:
    """``secagent doctor --fix``: pre-create + harden (0700) the token-cache
    directories (service + per-user) so a first `secagent login` / `secagent token`
    doesn't have to. Idempotent — mirrors the mkdir+harden_path a cache write already
    does inline (secsso.py `_write_cache`/`_write_user_cache`) when the dir is
    missing. Returns the directories touched, sorted for stable output."""
    dirs = {
        Path(settings.secsso.token_cache_path).expanduser().parent,
        Path(settings.secsso.user_token_cache_path).expanduser().parent,
    }
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
        harden_path(d, 0o700)
    return sorted(dirs)


def check_secrouter_models_endpoint(settings: Settings, probe: bool) -> Check:
    """Best-effort reachability of SecRouter's model listing — lighter than
    `check_llm_endpoint` (no generation, no requirement that `llm.api_key` resolve to
    a valid credential): useful right after `secagent init`, before `secagent login`.
    """
    if not probe:
        return Check("secrouter_models", True, "skipped (pass --probe)", severity="info")
    url = settings.llm.base_url.rstrip("/") + "/models"
    headers: dict[str, str] = {}
    # Best-effort: an unresolvable api_key (e.g. not logged in yet) still lets this
    # check test raw reachability unauthenticated — that is its whole point, unlike
    # check_llm_endpoint, which needs real auth to mean anything.
    with contextlib.suppress(SecretResolutionError):
        headers = {"Authorization": f"Bearer {resolve_secret(settings.llm.api_key)}"}
    try:
        resp = httpx.get(url, headers=headers, timeout=15.0, verify=True)
    except Exception as exc:  # noqa: BLE001 - report any connection issue
        return Check("secrouter_models", False,
                     f"unreachable: {url} ({exc.__class__.__name__})", severity="warn")
    return Check("secrouter_models", True,
                 f"reachable: {url} (HTTP {resp.status_code})", severity="info")


def check_secsso_device_endpoints(settings: Settings, probe: bool) -> Check:
    """Best-effort reachability of SecSSO's device-authorization + token endpoints
    (the two `secagent login` needs) — set by `secagent init --domain ...`. A GET
    (not the real POST flow) is deliberate: this is a reachability smoke test, not an
    exercise of the flow — it must not mint real device codes on every doctor run.
    """
    dev_url, tok_url = settings.secsso.device_authorization_url, settings.secsso.token_url
    if not dev_url and not tok_url:
        return Check("secsso_device_endpoints", True,
                     "not configured (run `secagent init --domain <domain>`)", severity="info")
    if not probe:
        return Check("secsso_device_endpoints", True,
                     f"configured: device={dev_url or '(unset)'}, token={tok_url or '(unset)'}",
                     severity="info")
    problems: list[str] = []
    reached: list[str] = []
    for label, url in (("device_authorization_url", dev_url), ("token_url", tok_url)):
        if not url:
            continue
        try:
            resp = httpx.get(url, timeout=15.0, verify=True)
            reached.append(f"{label} (HTTP {resp.status_code})")
        except Exception as exc:  # noqa: BLE001 - report any connection issue
            problems.append(f"{label} ({url}) unreachable: {exc.__class__.__name__}")
    if problems:
        detail = "; ".join(problems)
        if reached:
            detail += " | reachable: " + ", ".join(reached)
        return Check("secsso_device_endpoints", False, detail, severity="warn")
    return Check("secsso_device_endpoints", True, "reachable: " + ", ".join(reached),
                 severity="info")


def check_llm_endpoint(settings: Settings, probe: bool) -> Check:
    """Verify the LLM endpoint actually *generates*, not merely that it answers.

    Listing models is not evidence the configured model works: a wrong/evicted model name
    still returns 200 on ``/models`` while every real call 404s, and a reasoning model can
    return 200 with EMPTY ``content`` when the output budget is consumed by
    ``reasoning_content``. Both failures are silent — secagent's callers treat an empty
    completion as "nothing to report" — so ``--probe`` sends a real completion and
    asserts non-empty content.
    """
    base = settings.llm.base_url.rstrip("/")
    if not probe:
        return Check("llm_endpoint", True, f"configured: {settings.llm.base_url}", severity="info")
    url = base + "/chat/completions"
    try:
        # `llm.api_key` may be a "!command" (e.g. "!secagent token --user", the
        # per-user default `secagent init` writes — see secretval.py) that must be
        # RESOLVED before use, not sent verbatim: sending the literal string
        # "!secagent token --user" as a bearer value is a real generation failure
        # this probe exists to catch, not a hypothetical one.
        api_key = resolve_secret(settings.llm.api_key)
    except SecretResolutionError as exc:
        return Check("llm_endpoint", False,
                     f"could not resolve llm.api_key ({exc}) — is `secagent login` "
                     "needed first?", severity="error")
    headers = {"Authorization": f"Bearer {api_key}"}
    payload = {
        "model": settings.llm.model,
        "messages": [{"role": "user", "content": "Reply with the single word: READY"}],
        # Generous: reasoning models spend output tokens before emitting content.
        "max_tokens": 256,
        "temperature": 0,
    }
    try:
        resp = httpx.post(url, headers=headers, json=payload, timeout=60.0, verify=True)
    except Exception as exc:  # noqa: BLE001 - report any connection issue
        return Check("llm_endpoint", False,
                     f"unreachable: {url} ({exc.__class__.__name__})", severity="warn")
    if resp.status_code >= 400:
        detail = resp.text.strip()[:160]
        return Check("llm_endpoint", False,
                     f"model {settings.llm.model!r} rejected ({resp.status_code}): {detail}",
                     severity="error")
    try:
        data = resp.json()
        choice = data["choices"][0]
        content = (choice["message"].get("content") or "").strip()
        finish = choice.get("finish_reason", "")
    except Exception:  # noqa: BLE001 - any shape we don't recognize is a failure
        return Check("llm_endpoint", False,
                     f"unrecognized response shape from {url}", severity="error")
    if not content:
        hint = ""
        if finish == "length":
            hint = (" — output budget was consumed before any content (reasoning model?); "
                    "raise llm.max_output_tokens")
        return Check("llm_endpoint", False,
                     f"model {settings.llm.model!r} returned EMPTY content "
                     f"(finish_reason={finish!r}){hint}", severity="error")
    return Check("llm_endpoint", True,
                 f"{settings.llm.model} generated ok ({len(content)} chars)", severity="info")


def probe_context_window(settings: Settings) -> tuple[int | None, str]:
    """Ask the server what context window it actually serves for the configured model.

    Two dialects cover the local stacks secagent targets: LM Studio exposes
    ``/api/v0/models`` with ``max_context_length`` / ``loaded_context_length``, Ollama
    exposes ``/api/show``. Returns ``(tokens, source)``; ``(None, reason)`` when the
    server does not say, which is not an error — plenty of endpoints do not.
    """
    root = settings.llm.base_url.rstrip("/").removesuffix("/v1")
    model = settings.llm.model

    try:  # LM Studio
        r = httpx.get(f"{root}/api/v0/models", timeout=15.0)
        if r.status_code < 400:
            for m in r.json().get("data", []):
                if m.get("id") == model:
                    # The LOADED length is the real ceiling: a model served at 32k cannot
                    # use the 262k it is capable of.
                    loaded = m.get("loaded_context_length")
                    if loaded:
                        return int(loaded), "server (loaded)"
                    if m.get("max_context_length"):
                        return int(m["max_context_length"]), "server (max)"
    except Exception:  # noqa: BLE001 - discovery is best-effort
        pass

    try:  # Ollama
        r = httpx.post(f"{root}/api/show", json={"model": model}, timeout=15.0)
        if r.status_code < 400:
            info = r.json().get("model_info", {}) or {}
            for k, v in info.items():
                if k.endswith(".context_length") and v:
                    return int(v), "server"
    except Exception:  # noqa: BLE001
        pass

    return None, "server did not advertise a context window"


def check_llm_context_window(settings: Settings, probe: bool) -> Check:
    """Compare the configured window against what the server really serves.

    Getting this wrong is silent in both directions. Too low and secagent quietly uses a
    fraction of the context it paid for — and, worse, the empty-content retry ceiling is
    derived from it, so a reasoning model can never be given the budget it needs to emit
    any content at all. Too high and prompts overflow at the server.
    """
    cfg = settings.llm.context_window
    if not probe:
        return Check("llm_context_window", True, f"configured: {cfg} tokens",
                     severity="info")
    served, source = probe_context_window(settings)
    if served is None:
        return Check("llm_context_window", True,
                     f"configured: {cfg} tokens ({source})", severity="info")
    # Compare what secagent will actually SEND, not the nominal window. Only
    # `context_budget_ratio` of the window is ever filled (prompt budget + output
    # budget), so a window set slightly above what a server happens to have loaded is
    # harmless — and erroring on it would fire on a perfectly working setup, which is
    # how a check stops being read.
    peak = settings.llm.context_budget_tokens + settings.llm.max_output_tokens
    if peak > served:
        return Check("llm_context_window", False,
                     f"llm.context_window={cfg} means prompts of up to {peak} tokens, "
                     f"more than the {served} the {source} serves for "
                     f"{settings.llm.model!r}; they will overflow",
                     severity="error")
    if cfg * 4 <= served:
        return Check("llm_context_window", False,
                     f"llm.context_window={cfg} but the {source} serves {served} tokens "
                     f"for {settings.llm.model!r} — secagent is using "
                     f"{cfg * 100 // served}% of the available context, and empty-content "
                     f"retries are capped at {cfg // 2} tokens (reasoning models commonly "
                     f"need >12000 before emitting any content). Set "
                     f"SECAGENT_LLM__CONTEXT_WINDOW={served}",
                     severity="warn")
    return Check("llm_context_window", True,
                 f"{cfg} configured (peak {peak} tokens), {served} served",
                 severity="info")


def check_analysis_backends(settings: Settings) -> Check:
    """Report which optional code-analysis backends are actually usable.

    These are what make C/C++, C#, and Rust more than regex guesses: libclang drives the
    C/C++ call map, the tree-sitter grammars drive C#/Rust. Missing ones degrade silently
    at index time (zero functions, empty call map), so name them here.
    """
    present: list[str] = []
    missing: list[str] = []

    from .affordances.clang_ast import clang_available
    from .affordances.csharp_ast import csharp_available
    from .affordances.rust_ast import rust_available

    for label, available, extra in (
        ("libclang (C/C++ call map)", clang_available, "clang"),
        ("tree-sitter C#", csharp_available, "csharp"),
        ("tree-sitter Rust", rust_available, "rust"),
    ):
        (present if available() else missing).append(
            label if available() else f"{label} [pip install 'secagent[{extra}]']"
        )

    if not missing:
        return Check("analysis_backends", True, "all present: " + ", ".join(present))
    detail = "missing: " + "; ".join(missing)
    if present:
        detail += " | present: " + ", ".join(present)
    # Not an error: secagent still indexes with regex symbols. But it is a real capability
    # loss the user should know about before wondering why `callers` is empty.
    return Check("analysis_backends", True, detail, severity="warn")


def check_heavy_analysis_backends(settings: Settings) -> Check:
    """Report whether the heavy (compiled) analysis backend container images are usable.

    The light backends (`check_analysis_backends`) are pip packages; the heavy ones —
    the Roslyn C# analyzer and rust-analyzer Rust analyzer used by `analyze deep` — are
    opt-in container images (see docs/design/heavy-analysis-pipeline.md). An evaluation
    run had both images built and working and doctor said nothing about them; a user with
    neither gets a clean bill of health from the rest of doctor and then wonders why
    `analyze deep` does nothing. This probes with the SAME function ``heavy.py`` itself
    uses to decide whether to run (`image_available`), so "present" here means the exact
    thing that would let a heavy run actually start — not merely that a name is
    configured.
    """
    from .affordances.heavy import image_available

    runtime = settings.affordances.analyzer_runtime
    targets = (
        ("Roslyn C# analyzer image", settings.affordances.analyzer_image_dotnet,
         "make analyzer-dotnet"),
        ("rust-analyzer image", settings.affordances.analyzer_image_rust,
         "make analyzer-rust"),
    )
    present: list[str] = []
    missing: list[str] = []
    for label, image, build_cmd in targets:
        if image_available(runtime, image):
            present.append(f"{label} ({image})")
        else:
            missing.append(f"{label} ({image}) [{build_cmd}]")

    if not missing:
        return Check("heavy_analysis_backends", True, "all present: " + ", ".join(present))
    detail = "missing: " + "; ".join(missing)
    if present:
        detail += " | present: " + ", ".join(present)
    # Same discipline as the light check: secagent still indexes/analyzes via the light
    # path without these, so a missing heavy image is a capability loss, not a failure.
    return Check("heavy_analysis_backends", True, detail, severity="warn")


def check_audit(settings: Settings) -> Check:
    if settings.audit.enabled:
        from .audit import verify_chain

        ok, message = verify_chain(settings.audit.path)
        # A missing log is fine (nothing recorded yet); a tampered one is fatal.
        if ok or "no audit log" in message:
            return Check("audit", True, f"enabled -> {settings.audit.path} ({message})")
        return Check("audit", False, f"audit log integrity FAILED: {message}")
    # Disabled is a warning under FIPS/CUI operation, info otherwise.
    sev = "warn" if settings.fips.require_fips else "info"
    return Check("audit", True, "audit logging disabled (enable for CMMC L2)", severity=sev)


def check_network(settings: Settings) -> Check:
    net = settings.network
    if not net.require_tls and not net.allowed_hosts:
        return Check("network", True, "egress policy not enforced (require_tls=false)",
                     severity="info")
    from .netpolicy import check_endpoint

    problems = []
    for url in (settings.llm.base_url, settings.gitlab.url):
        ok, reason = check_endpoint(url, net)
        if not ok:
            problems.append(reason)
    if problems:
        return Check("network", False, "; ".join(problems))
    detail = "require_tls" if net.require_tls else ""
    if net.allowed_hosts:
        detail += f" allow_hosts={','.join(net.allowed_hosts)}"
    return Check("network", True, f"egress policy ok ({detail.strip()})")


def run_doctor(settings: Settings, probe_endpoint: bool = False) -> list[Check]:
    """Run all checks and return them in display order."""
    checks = [
        check_python_version(),
        check_secagent_version(),
        check_openssl(),
        check_fips(settings.fips.require_fips, settings.fips.allow_non_fips),
        check_no_weak_hashes(),
        check_audit(settings),
        check_network(settings),
        check_diagram_renderer(settings),
        check_ikos(),
        check_analysis_backends(settings),
        check_heavy_analysis_backends(settings),
        check_docs_extra(),
        check_pi_runtime(),
        check_node_runtime(),
        check_onboarding(),
        check_user_login(settings),
        check_llm_endpoint(settings, probe_endpoint),
        check_llm_context_window(settings, probe_endpoint),
        check_secrouter_models_endpoint(settings, probe_endpoint),
        check_secsso_device_endpoints(settings, probe_endpoint),
    ]
    return checks


def doctor_failed(checks: list[Check]) -> bool:
    return any((not c.ok) and c.severity == "error" for c in checks)
