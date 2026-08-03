"""Structured, attributable, tamper-evident audit logging (CMMC-1).

Addresses NIST SP 800-171 / CMMC L2 audit & accountability controls (AU.L2-3.3.1,
3.3.2, 3.3.4, 3.3.8). Every agent action — indexing, docs generation, MR reviews and
replies, and MCP tool calls — is recorded as one append-only JSONL line, chained with
a SHA-256 (FIPS-approved) hash so any insertion, deletion, or edit is detectable.

The logger is a no-op when disabled (the default), so it adds nothing unless an
operator opts in via ``audit.enabled`` / ``SECAGENT_AUDIT__ENABLED=true``. It never
records secrets, and credentials are stripped from any URL before logging.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .security import harden_path, text_hash

GENESIS_HASH = "0" * 64
SCHEMA_VERSION = 1


def _utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def redact_url(url: str) -> str:
    """Strip any userinfo (user:password@) from a URL before logging."""
    if not url:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<unparseable-url>"
    if parts.username or parts.password:
        netloc = parts.hostname or ""
        if parts.port:
            netloc = f"{netloc}:{parts.port}"
        parts = parts._replace(netloc=netloc)
    return urlunsplit(parts)


def _canonical(record: dict[str, Any]) -> str:
    """Stable serialization for hashing (key order independent)."""
    return json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)


@dataclass
class AuditEvent:
    action: str
    target: dict[str, Any]
    model: str = ""
    endpoint: str = ""
    outcome: str = "ok"  # ok | error
    detail: str = ""


class AuditLogger:
    """Append-only, hash-chained JSONL audit logger.

    Use :func:`get_audit_logger` to build one from settings. A disabled logger is a
    safe no-op. Writes are serialized with a lock; failures degrade to stderr and
    never raise into the calling agent.
    """

    def __init__(
        self,
        path: str | Path | None,
        *,
        enabled: bool = True,
        principal: str = "service:secagent",
        echo_stderr: bool = False,
    ) -> None:
        self.enabled = enabled
        self.principal = principal
        self.echo_stderr = echo_stderr
        self.run_id = uuid.uuid4().hex
        self._seq = 0
        self._lock = threading.Lock()
        self._path = Path(path) if path else None
        self._prev_hash = GENESIS_HASH
        if self.enabled and self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            harden_path(self._path.parent, 0o700)  # at-rest hardening (CMMC-2)
            self._prev_hash = _last_hash(self._path) or GENESIS_HASH

    # -- recording -----------------------------------------------------------
    def record(
        self,
        action: str,
        *,
        target: dict[str, Any] | None = None,
        model: str = "",
        endpoint: str = "",
        outcome: str = "ok",
        detail: str = "",
    ) -> dict[str, Any] | None:
        """Append one audit record. Returns the written record (or None if disabled)."""
        if not self.enabled:
            return None
        with self._lock:
            self._seq += 1
            record = {
                "v": SCHEMA_VERSION,
                "ts": _utc_now_iso(),
                "run_id": self.run_id,
                "seq": self._seq,
                "principal": self.principal,
                "action": action,
                "target": target or {},
                "model": model,
                "endpoint": redact_url(endpoint),
                "outcome": outcome,
                "detail": detail[:500],
                "prev_hash": self._prev_hash,
            }
            digest = text_hash(_canonical(record))
            record["hash"] = digest
            self._prev_hash = digest
            self._write(record)
            return record

    def event(self, ev: AuditEvent) -> dict[str, Any] | None:
        return self.record(
            ev.action, target=ev.target, model=ev.model, endpoint=ev.endpoint,
            outcome=ev.outcome, detail=ev.detail,
        )

    def _write(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, default=str)
        try:
            if self._path is not None:
                with open(self._path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
                harden_path(self._path, 0o600)  # owner-only audit log (CMMC-2)
            if self.echo_stderr or self._path is None:
                print(f"AUDIT {line}", file=sys.stderr)
        except OSError as exc:  # never break the agent on a logging failure
            print(f"AUDIT-ERROR could not write audit record: {exc}", file=sys.stderr)


def get_audit_logger(settings) -> AuditLogger:
    """Build an :class:`AuditLogger` from settings (no-op when disabled)."""
    cfg = settings.audit
    principal = cfg.principal or os.environ.get("SECAGENT_PRINCIPAL") or "service:secagent"
    # Path is used as-is (relative to cwd unless absolute); operators should set an
    # absolute path on a protected, SIEM-forwarded volume.
    path = Path(cfg.path) if cfg.enabled else None
    return AuditLogger(
        path,
        enabled=cfg.enabled,
        principal=principal,
        echo_stderr=cfg.echo_stderr,
    )


def _last_hash(path: Path) -> str | None:
    """Return the hash of the last record in an existing log, to continue the chain."""
    if not path.exists():
        return None
    last = None
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    last = line
    except OSError:
        return None
    if not last:
        return None
    try:
        value = json.loads(last).get("hash")
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, str) else None


def verify_chain(path: str | Path) -> tuple[bool, str]:
    """Validate a JSONL audit log's hash chain.

    Returns ``(ok, message)``. Detects edited records (hash mismatch) and broken
    linkage (a record whose ``prev_hash`` does not match the prior record's hash).
    """
    p = Path(path)
    if not p.exists():
        return False, f"no audit log at {p}"
    prev = GENESIS_HASH
    n = 0
    with open(p, encoding="utf-8") as fh:
        for i, raw in enumerate(fh, start=1):
            raw = raw.strip()
            if not raw:
                continue
            n += 1
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                return False, f"line {i}: invalid JSON"
            stored = record.get("hash")
            if stored is None:
                return False, f"line {i}: missing hash"
            if record.get("prev_hash") != prev:
                return False, f"line {i}: broken chain linkage (prev_hash mismatch)"
            recomputed = text_hash(_canonical({k: v for k, v in record.items() if k != "hash"}))
            if recomputed != stored:
                return False, f"line {i}: record hash mismatch (tampered)"
            prev = str(stored)
    return True, f"verified {n} record(s)"
