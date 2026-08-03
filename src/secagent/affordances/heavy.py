"""Run the optional heavy (compiled) analysis backends in their containers.

These backends actually build the project for fully-resolved semantic data and live in
opt-in container images (see ``docs/design/heavy-analysis-pipeline.md``). This module
invokes the image, captures its ``secagent-analysis/v1`` report, and returns it for the
shared ingest. Everything degrades gracefully: a missing runtime/image, no project, or a
bad run returns ``None`` so callers fall back to the light path.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path

from .analysis import AnalysisReport, parse_report

log = logging.getLogger(__name__)


def _find_csharp_target(repo: Path) -> str | None:
    """Repo-relative path to a `.sln` (preferred) or `.csproj`, or None."""
    slns = sorted(repo.rglob("*.sln"))
    if slns:
        return slns[0].relative_to(repo).as_posix()
    projs = sorted(repo.rglob("*.csproj"))
    return projs[0].relative_to(repo).as_posix() if projs else None


def _find_rust_target(repo: Path) -> str | None:
    """Repo-relative dir of the crate/workspace root (the nearest-to-root Cargo.toml)."""
    manifests = sorted(repo.rglob("Cargo.toml"), key=lambda p: len(p.parts))
    if not manifests:
        return None
    return manifests[0].parent.relative_to(repo).as_posix() or "."


def image_available(runtime: str, image: str) -> bool:
    """True if the container runtime is on PATH and the image is present locally."""
    if not shutil.which(runtime):
        return False
    try:
        r = subprocess.run([runtime, "image", "inspect", image],
                           capture_output=True, timeout=30)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def run_dotnet_analyzer(repo: str | Path, settings, timeout: int = 600) -> AnalysisReport | None:
    """Run the C# Roslyn analyzer container over ``repo`` and return its report.

    Offline (`--network none`), with the source mounted read-only. Returns None (and logs
    why) when the runtime/image is unavailable, no C# project is found, or the run fails.
    """
    repo = Path(repo).resolve()
    cfg = settings.affordances
    runtime, image = cfg.analyzer_runtime, cfg.analyzer_image_dotnet

    if not image_available(runtime, image):
        log.info("heavy C# backend unavailable (no %s image '%s') — run `make analyzer-dotnet`",
                 runtime, image)
        return None
    target = _find_csharp_target(repo)
    if target is None:
        log.info("heavy C#: no .sln/.csproj found under %s", repo)
        return None

    cmd = [runtime, "run", "--rm", "--network", "none",
           "-v", f"{repo}:/src:ro", image, target]
    log.info("heavy C#: analyzing %s (offline)…", target)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("heavy C# analyzer failed to run: %s", exc)
        return None
    if proc.returncode != 0:
        log.warning("heavy C# analyzer exited %d: %s", proc.returncode, proc.stderr[-500:].strip())
        return None
    try:
        report = parse_report(json.loads(proc.stdout))
    except (json.JSONDecodeError, ValueError) as exc:
        log.warning("heavy C# analyzer produced invalid output: %s", exc)
        return None
    if not report.build.get("restored", True):
        log.warning("heavy C#: project was not fully restored; results are partial. "
                    "Restore/build it first (offline mode does not restore).")
    return report


def run_rust_analyzer(repo: str | Path, settings, timeout: int = 600) -> AnalysisReport | None:
    """Run the Rust (rust-analyzer) analyzer container over ``repo`` and return its report.

    Offline (`--network none`), source mounted read-only. Returns None (and logs why) when
    the runtime/image is unavailable, no Cargo project is found, or the run fails. Like the
    C# path, offline analysis assumes dependencies are pre-fetched (`cargo fetch`/vendored);
    otherwise rust-analyzer resolves only what's available and the report notes it.
    """
    repo = Path(repo).resolve()
    cfg = settings.affordances
    runtime, image = cfg.analyzer_runtime, cfg.analyzer_image_rust

    if not image_available(runtime, image):
        log.info("heavy Rust backend unavailable (no %s image '%s') — run `make analyzer-rust`",
                 runtime, image)
        return None
    target = _find_rust_target(repo)
    if target is None:
        log.info("heavy Rust: no Cargo.toml found under %s", repo)
        return None

    cmd = [runtime, "run", "--rm", "--network", "none",
           "-v", f"{repo}:/src:ro", image, target]
    log.info("heavy Rust: analyzing %s (offline)…", target)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("heavy Rust analyzer failed to run: %s", exc)
        return None
    if proc.returncode != 0:
        log.warning("heavy Rust analyzer exited %d: %s", proc.returncode,
                    proc.stderr[-500:].strip())
        return None
    try:
        report = parse_report(json.loads(proc.stdout))
    except (json.JSONDecodeError, ValueError) as exc:
        log.warning("heavy Rust analyzer produced invalid output: %s", exc)
        return None
    if not report.build.get("restored", True):
        log.warning("heavy Rust: dependencies were not fully resolved; results are partial. "
                    "`cargo fetch` (or vendor) first (offline mode does not fetch).")
    return report


def run_heavy_analyzer(repo: str | Path, settings, timeout: int = 600) -> AnalysisReport | None:
    """Run the heavy backend matching the repo's language, or None if none applies.

    Dispatch by project marker: a ``Cargo.toml`` selects the Rust backend, a
    ``.sln``/``.csproj`` the C# backend. Each degrades gracefully to None (missing image,
    no project, failed run) so the caller falls back to the light path.
    """
    repo = Path(repo).resolve()
    if _find_rust_target(repo) is not None:
        report = run_rust_analyzer(repo, settings, timeout)
        if report is not None:
            return report
    if _find_csharp_target(repo) is not None:
        return run_dotnet_analyzer(repo, settings, timeout)
    return None
