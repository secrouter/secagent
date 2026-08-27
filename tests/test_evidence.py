"""Tests for the `secagent evidence` CMMC evidence bundle (Spec B.6)."""

from __future__ import annotations

import json
import re

from typer.testing import CliRunner

from secagent.audit import AuditLogger
from secagent.cli import app
from secagent.config import Settings
from secagent.evidence import build_evidence_bundle, write_evidence_bundle

runner = CliRunner()

TOP_LEVEL_KEYS = {
    "product", "version", "generatedAt", "generatedBy", "config",
    "auditChain", "auditRecent", "controls",
}

# Spec B.5: bare Family + dotted NIST SP 800-171 r2 ID (no "AU.L2-" style prefix).
_FAMILY_RE = re.compile(r"^[A-Z]{2}$")
_ID_RE = re.compile(r"^\d+\.\d+(\.\d+)?$")


def _settings(tmp_path) -> Settings:
    s = Settings()
    s.audit.path = str(tmp_path / "audit.jsonl")
    return s


def test_bundle_has_required_top_level_keys(tmp_path):
    bundle = build_evidence_bundle(_settings(tmp_path))
    assert set(bundle) >= TOP_LEVEL_KEYS
    assert bundle["product"] == "secagent"
    assert isinstance(bundle["version"], str) and bundle["version"]
    assert isinstance(bundle["generatedBy"], str) and bundle["generatedBy"]


def test_bundle_is_json_serializable(tmp_path):
    bundle = build_evidence_bundle(_settings(tmp_path))
    # Must round-trip cleanly -- this is the whole contract of "evidence bundle".
    reparsed = json.loads(json.dumps(bundle))
    assert reparsed["product"] == "secagent"


def test_audit_disabled_reports_disabled_chain_and_empty_recent(tmp_path):
    s = _settings(tmp_path)
    s.audit.enabled = False
    bundle = build_evidence_bundle(s)
    assert bundle["auditChain"] == {"enabled": False}
    assert bundle["auditRecent"] == []


def test_audit_enabled_verifies_chain_and_includes_recent(tmp_path):
    s = _settings(tmp_path)
    s.audit.enabled = True
    logger = AuditLogger(s.audit.path, enabled=True, principal="service:test")
    for i in range(5):
        logger.record("index", target={"i": i})
    bundle = build_evidence_bundle(s)
    chain = bundle["auditChain"]
    assert chain["enabled"] is True
    assert chain["ok"] is True
    assert chain["checked"] == 5
    assert len(bundle["auditRecent"]) == 5
    assert bundle["auditRecent"][0]["action"] == "index"


def test_audit_enabled_but_no_log_yet_is_not_a_failure(tmp_path):
    s = _settings(tmp_path)
    s.audit.enabled = True
    bundle = build_evidence_bundle(s)
    chain = bundle["auditChain"]
    assert chain["enabled"] is True
    assert chain["ok"] is True
    assert chain["checked"] == 0
    assert bundle["auditRecent"] == []


def test_audit_recent_capped_at_200(tmp_path):
    s = _settings(tmp_path)
    s.audit.enabled = True
    logger = AuditLogger(s.audit.path, enabled=True)
    for i in range(210):
        logger.record("index", target={"i": i})
    bundle = build_evidence_bundle(s)
    assert len(bundle["auditRecent"]) == 200
    # Most recent records are kept, not the oldest.
    assert bundle["auditRecent"][-1]["target"]["i"] == 209


def test_tampered_chain_is_reported_broken(tmp_path):
    s = _settings(tmp_path)
    s.audit.enabled = True
    logger = AuditLogger(s.audit.path, enabled=True)
    logger.record("index", target={"repo": "a"})
    logger.record("index", target={"repo": "b"})
    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    rec = json.loads(lines[0])
    rec["target"] = {"repo": "EVIL"}
    lines[0] = json.dumps(rec)
    (tmp_path / "audit.jsonl").write_text("\n".join(lines) + "\n")

    bundle = build_evidence_bundle(s)
    chain = bundle["auditChain"]
    assert chain["ok"] is False
    assert chain["brokenAtId"] == 1


def test_config_posture_is_sanitized_never_leaks_a_token(tmp_path):
    s = _settings(tmp_path)
    token = "glpat-super-secret-token-should-never-leak-1234567890"  # noqa: S105
    s.gitlab.token = token
    s.gitlab.webhook_secret = "another-secret-value-xyz"
    s.llm.api_key = "sk-another-secret-key-abcdef"
    bundle = build_evidence_bundle(s)
    dumped = json.dumps(bundle)
    assert token not in dumped
    assert "another-secret-value-xyz" not in dumped
    assert "sk-another-secret-key-abcdef" not in dumped
    # Only booleans/hosts/paths for gitlab -- never the raw secret fields.
    assert bundle["config"]["gitlab"] == {"url": s.gitlab.url, "webhook_configured": True}
    assert "token" not in bundle["config"]["gitlab"]
    assert "webhook_secret" not in bundle["config"]["gitlab"]


def test_controls_match_b5_id_format(tmp_path):
    bundle = build_evidence_bundle(_settings(tmp_path))
    controls = bundle["controls"]
    assert controls, "control self-assessment must not be empty"
    for control in controls:
        assert set(control) >= {"id", "family", "status", "evidence"}
        assert _FAMILY_RE.match(control["family"]), control
        assert _ID_RE.match(control["id"]), control
        assert control["status"] in {"met", "partial", "gap"}
        assert control["evidence"]


def test_write_evidence_bundle_writes_file_and_returns_path(tmp_path):
    s = _settings(tmp_path)
    out_dir = tmp_path / "out"
    path = write_evidence_bundle(s, out_dir=out_dir)
    assert path.exists()
    assert path.name.startswith("secagent-evidence-")
    assert path.name.endswith(".json")
    data = json.loads(path.read_text())
    assert set(data) >= TOP_LEVEL_KEYS


def test_cli_evidence_command_prints_output_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["evidence"])
    assert result.exit_code == 0, result.output
    assert "secagent-evidence-" in result.output
    written = list(tmp_path.glob("secagent-evidence-*.json"))
    assert len(written) == 1
    data = json.loads(written[0].read_text())
    assert set(data) >= TOP_LEVEL_KEYS


def test_cli_evidence_command_respects_out_dir(tmp_path):
    out_dir = tmp_path / "evidence-out"
    result = runner.invoke(app, ["evidence", "--out", str(out_dir)])
    assert result.exit_code == 0, result.output
    written = list(out_dir.glob("secagent-evidence-*.json"))
    assert len(written) == 1
