"""CMMC self-assessment evidence bundle (Spec B.6).

``secagent evidence`` writes a single JSON bundle an assessor (or a CI job) can
collect without touching the deployment directly: whether audit logging is on and
its hash chain still verifies, a SANITIZED slice of the effective configuration
(booleans / hostnames / paths only -- never secrets), the last N audit records
(already metadata-only -- see ``audit.py``), and a small self-assessment against the
controls documented in :doc:`cmmc` (bare NIST SP 800-171 Family + dotted ID, per
Spec B.5 -- see that doc's citation-style note).

This is evidence for an assessor to review, not a certification -- see
docs/cmmc.md's shared-responsibility warning.
"""

from __future__ import annotations

import contextlib
import getpass
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__
from .config import Settings

PRODUCT = "secagent"


def _generated_by() -> str:
    """Best-effort current OS user. Never raises -- an odd/sandboxed environment
    without a resolvable user must not break evidence export over an attribution nicety."""
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 - environment-dependent, never fatal here
        return "unknown"


def config_posture(settings: Settings) -> dict[str, Any]:
    """A SANITIZED slice of the effective config: booleans, hostnames/URLs, and paths
    only. Deliberately a hand-picked projection rather than a ``safe_dict()`` dump --
    ``safe_dict`` only redacts the *known* secret fields (``llm.api_key``,
    ``gitlab.token``, ``gitlab.webhook_secret``), so a token placed in some other
    field would still leak through it. Nothing selected here is ever secret-shaped.
    """
    net = settings.network
    audit = settings.audit
    fips = settings.fips
    lc = settings.leanctx
    return {
        "audit": {"enabled": audit.enabled, "path": audit.path},
        "network": {"require_tls": net.require_tls, "allowed_hosts": list(net.allowed_hosts)},
        "fips": {"require_fips": fips.require_fips, "allow_non_fips": fips.allow_non_fips},
        "llm": {"base_url": settings.llm.base_url},
        "gitlab": {"url": settings.gitlab.url,
                   "webhook_configured": bool(settings.gitlab.webhook_secret)},
        "marking": {"banner_set": bool(settings.marking.banner)},
        "leanctx": {"enabled": lc.enabled, "endpoint": lc.endpoint,
                    "is_loopback": lc.is_loopback, "persist_context": lc.persist_context},
        "affordances": {"store_dir": settings.affordances.store_dir},
    }


def audit_chain_status(settings: Settings) -> dict[str, Any]:
    """``{"enabled": false}`` when audit logging is off; otherwise verify the chain and
    report ``{enabled, ok, checked, message, brokenAtId?}`` per Spec B.6.

    A missing log (nothing recorded yet) is reported as ``ok: true, checked: 0`` --
    the same non-failure treatment ``doctor.check_audit`` gives it -- rather than as
    tampering.
    """
    if not settings.audit.enabled:
        return {"enabled": False}

    from .audit import verify_chain

    ok, message = verify_chain(settings.audit.path)
    if not ok and message.startswith("no audit log"):
        return {"enabled": True, "ok": True, "checked": 0, "message": message}

    result: dict[str, Any] = {"enabled": True, "ok": ok, "checked": 0, "message": message}
    if ok:
        # "verified N record(s)" -- see audit.verify_chain.
        with contextlib.suppress(IndexError, ValueError):
            result["checked"] = int(message.split()[1])
    else:
        # "line N: ..." -- see audit.verify_chain. N is the 1-based line (record)
        # where the chain broke; everything before it verified fine.
        with contextlib.suppress(IndexError, ValueError):
            broken_at = int(message.split(":", 1)[0].removeprefix("line").strip())
            result["brokenAtId"] = broken_at
            result["checked"] = broken_at - 1
    return result


def audit_recent(settings: Settings, limit: int = 200) -> list[dict[str, Any]]:
    """Last ``limit`` audit records, already metadata-only (see ``audit.py``'s
    "never log content" discipline). Empty when audit logging is disabled or nothing
    has been recorded yet.
    """
    if not settings.audit.enabled:
        return []
    path = Path(settings.audit.path)
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                records.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
    return records[-limit:]


@dataclass
class ControlAssessment:
    """One self-assessment row. ``family``/``id`` follow Spec B.5's doc-table style:
    a bare Family code and a bare dotted NIST SP 800-171 r2 ID (no ``AU.L2-`` prefix)."""

    id: str
    family: str
    status: str  # "met" | "partial" | "gap"
    evidence: str

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "family": self.family, "status": self.status,
                "evidence": self.evidence}


def control_self_assessment(settings: Settings, chain: dict[str, Any]) -> list[dict[str, str]]:
    """A small self-assessment mirroring docs/cmmc.md's implemented (Met) rows.

    Some rows are STATIC (design-level claims that hold regardless of runtime
    config); others are COMPUTED from the effective settings / the live
    ``audit_chain_status`` result so the bundle reflects what THIS deployment
    actually has configured, not merely what the code is capable of.
    """
    audit_ok = bool(chain.get("enabled")) and bool(chain.get("ok"))
    rows = [
        ControlAssessment(
            "3.3.1", "AU",
            "met" if settings.audit.enabled else "partial",
            "Append-only, hash-chained JSONL of agent/MCP actions "
            "(secagent/audit.py:AuditLogger); `secagent audit verify`, `secagent doctor`.",
        ),
        ControlAssessment(
            "3.3.2", "AU",
            "met" if settings.audit.enabled else "partial",
            "Each record carries `principal` + a per-process `run_id` "
            "(secagent/audit.py:AuditLogger.record).",
        ),
        ControlAssessment(
            "3.3.8", "AU",
            "met" if audit_ok else ("partial" if settings.audit.enabled else "gap"),
            "SHA-256 hash chain; insertion/deletion/edit detectable "
            "(secagent/audit.py:verify_chain). See auditChain in this bundle.",
        ),
        ControlAssessment(
            "3.1.3", "AC",
            "met" if (settings.network.require_tls or settings.network.allowed_hosts)
            else "partial",
            "Egress allow-list (`network.allowed_hosts`) + TLS enforcement "
            "(secagent/netpolicy.py).",
        ),
        ControlAssessment(
            "3.1.12", "AC",
            "met" if settings.gitlab.webhook_secret else "gap",
            "Constant-time webhook token check, optional source-IP allow-list + mTLS "
            "(secagent/agents/review/webhook.py).",
        ),
        ControlAssessment(
            "3.13.8", "SC",
            "met" if settings.network.require_tls else "partial",
            "TLS enforced in transit, loopback exempt "
            "(secagent/netpolicy.py:check_endpoint).",
        ),
        ControlAssessment(
            "3.13.11", "SC",
            "met" if settings.fips.require_fips else "partial",
            "SHA-256-only hashing surface; FIPS enforcement at startup "
            "(secagent/security.py:enforce_fips_policy).",
        ),
        ControlAssessment(
            "3.13.16", "SC",
            "met",
            "Affordance store + audit log created owner-only (0700/0600); volume "
            "encryption is the accepted baseline for at-rest encryption "
            "(secagent/security.py:harden_path).",
        ),
        ControlAssessment(
            "3.8.1", "MP",
            "met",
            "Owner-only affordance store + audit log permissions "
            "(secagent/security.py:harden_path).",
        ),
        ControlAssessment(
            "3.8.3", "MP",
            "met",
            "Secure delete: overwrite + unlink (`secagent purge`, "
            "secagent/affordances/api.py:purge_store).",
        ),
        ControlAssessment(
            "3.8.4", "MP",
            "met" if settings.marking.banner else "gap",
            "CUI marking banner on generated docs + MR comments "
            "(secagent/config.py:MarkingConfig.banner).",
        ),
        ControlAssessment(
            "3.4.1", "CM",
            "met",
            "CycloneDX SBOM generated in CI (`make sbom`).",
        ),
        ControlAssessment(
            "3.14.2", "SI",
            "met",
            "Untrusted code/diff/MR content wrapped in unguessable delimiters with "
            "breakout-stripping before reaching the model (secagent/sanitize.py).",
        ),
    ]
    return [r.to_dict() for r in rows]


def build_evidence_bundle(settings: Settings) -> dict[str, Any]:
    """Assemble the full JSON-serializable evidence bundle (Spec B.6 shape)."""
    chain = audit_chain_status(settings)
    return {
        "product": PRODUCT,
        "version": __version__,
        "generatedAt": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "generatedBy": _generated_by(),
        "config": config_posture(settings),
        "auditChain": chain,
        "auditRecent": audit_recent(settings),
        "controls": control_self_assessment(settings, chain),
    }


def write_evidence_bundle(settings: Settings, out_dir: str | Path = ".") -> Path:
    """Write the bundle to ``<out_dir>/secagent-evidence-<date>.json`` and return the
    path written (Spec B.6 naming: ``<component>-evidence-<date>.json``)."""
    bundle = build_evidence_bundle(settings)
    date = bundle["generatedAt"][:10]  # YYYY-MM-DD
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"secagent-evidence-{date}.json"
    out.write_text(json.dumps(bundle, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out
