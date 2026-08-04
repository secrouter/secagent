"""Tests for structured audit logging (CMMC-1)."""

from __future__ import annotations

import json
from pathlib import Path

from secagent.affordances.api import index_repo
from secagent.audit import AuditEvent, AuditLogger, get_audit_logger, redact_url, verify_chain
from secagent.config import Settings
from secagent.security import text_hash

FIXTURE = Path(__file__).parent / "fixtures" / "sample_repo"


def test_disabled_logger_is_noop(tmp_path):
    log = tmp_path / "audit.jsonl"
    logger = AuditLogger(log, enabled=False)
    assert logger.record("index", target={"x": 1}) is None
    assert not log.exists()


def test_record_writes_chained_jsonl(tmp_path):
    log = tmp_path / "audit.jsonl"
    logger = AuditLogger(log, enabled=True, principal="service:test")
    r1 = logger.record("index", target={"repo": "r"})
    r2 = logger.record("review_mr", target={"mr_iid": 7}, outcome="ok")
    assert r1 and r2
    assert r1["prev_hash"] == "0" * 64
    assert r2["prev_hash"] == r1["hash"]  # chained
    assert r1["principal"] == "service:test"
    lines = [json.loads(x) for x in log.read_text().splitlines()]
    assert [x["action"] for x in lines] == ["index", "review_mr"]
    assert all(len(x["hash"]) == 64 for x in lines)


def test_verify_chain_ok(tmp_path):
    log = tmp_path / "audit.jsonl"
    logger = AuditLogger(log, enabled=True)
    for i in range(5):
        logger.record("mcp_call", target={"i": i})
    ok, msg = verify_chain(log)
    assert ok, msg
    assert "verified 5" in msg


def test_verify_detects_tampering(tmp_path):
    log = tmp_path / "audit.jsonl"
    logger = AuditLogger(log, enabled=True)
    logger.record("index", target={"repo": "a"})
    logger.record("index", target={"repo": "b"})
    # Tamper with the first record's payload, leaving its hash intact.
    lines = log.read_text().splitlines()
    rec = json.loads(lines[0])
    rec["target"] = {"repo": "EVIL"}
    lines[0] = json.dumps(rec)
    log.write_text("\n".join(lines) + "\n")
    ok, msg = verify_chain(log)
    assert not ok
    assert "tampered" in msg or "hash mismatch" in msg


def test_verify_detects_deletion(tmp_path):
    log = tmp_path / "audit.jsonl"
    logger = AuditLogger(log, enabled=True)
    for i in range(3):
        logger.record("index", target={"i": i})
    lines = log.read_text().splitlines()
    del lines[1]  # remove a middle record -> breaks chain linkage
    log.write_text("\n".join(lines) + "\n")
    ok, msg = verify_chain(log)
    assert not ok
    assert "linkage" in msg


def test_chain_continues_across_instances(tmp_path):
    log = tmp_path / "audit.jsonl"
    AuditLogger(log, enabled=True).record("index", target={"n": 1})
    # A fresh logger should pick up the prior hash and keep the chain valid.
    AuditLogger(log, enabled=True).record("index", target={"n": 2})
    ok, _ = verify_chain(log)
    assert ok


def test_redact_url():
    assert redact_url("https://user:secret@host:8000/v1") == "https://host:8000/v1"
    assert redact_url("http://host/v1") == "http://host/v1"


def test_get_audit_logger_from_settings(tmp_path):
    s = Settings()
    s.audit.enabled = True
    s.audit.path = str(tmp_path / "a.jsonl")
    s.audit.principal = "service:ci"
    logger = get_audit_logger(s)
    assert logger.enabled and logger.principal == "service:ci"


def test_index_repo_emits_audit_record(tmp_path):
    s = Settings()
    s.affordances.llm_summaries = False
    s.affordances.store_dir = str(tmp_path / "store")
    s.audit.enabled = True
    s.audit.path = str(tmp_path / "audit.jsonl")
    index_repo(FIXTURE, s)
    ok, _ = verify_chain(s.audit.path)
    assert ok
    actions = [json.loads(x)["action"] for x in Path(s.audit.path).read_text().splitlines()]
    assert "index" in actions


# -- chat interaction events (UC101 Mattermost front end) --------------------------


def test_audit_config_capture_content_defaults_false():
    assert Settings().audit.capture_content is False


def test_get_audit_logger_capture_content_from_settings(tmp_path):
    s = Settings()
    s.audit.enabled = True
    s.audit.path = str(tmp_path / "a.jsonl")
    s.audit.capture_content = True
    logger = get_audit_logger(s)
    assert logger.capture_content is True


def test_record_chat_metadata_mode_hashes_content(tmp_path):
    """Default (capture_content=False): end_user + a hash are recorded, never the text."""
    log = tmp_path / "audit.jsonl"
    logger = AuditLogger(log, enabled=True, principal="service:secchat-bot")
    rec = logger.record_chat(
        "review_mr",
        end_user="mattermost:alice",
        channel="town-square",
        thread="thr-123",
        message="please review MR 42",
        reply="Looks good, one nit.",
    )
    assert rec is not None
    assert rec["principal"] == "service:secchat-bot"  # service identity, unchanged
    assert rec["end_user"] == "mattermost:alice"       # NEW: distinct end-user identity
    assert rec["cui"] is False
    assert rec["target"]["channel"] == "town-square"
    assert rec["target"]["thread"] == "thr-123"
    assert rec["target"]["message_sha256"] == text_hash("please review MR 42")
    assert rec["target"]["reply_sha256"] == text_hash("Looks good, one nit.")
    # No verbatim content anywhere in the record — metadata mode is CUI-free.
    assert "message" not in rec["target"]
    assert "reply" not in rec["target"]
    dumped = json.dumps(rec)
    assert "please review MR 42" not in dumped
    assert "Looks good, one nit." not in dumped


def test_record_chat_capture_content_includes_verbatim_and_tags_cui(tmp_path):
    log = tmp_path / "audit.jsonl"
    logger = AuditLogger(log, enabled=True, capture_content=True)
    rec = logger.record_chat(
        "analyze",
        end_user="mattermost:bob",
        channel="secops",
        thread="thr-9",
        message="analyze this repo",
        reply="Done, 3 findings.",
    )
    assert rec is not None
    assert rec["cui"] is True
    assert rec["target"]["message"] == "analyze this repo"
    assert rec["target"]["reply"] == "Done, 3 findings."
    # The digest is still present alongside the verbatim text (cheap, harmless, useful
    # for correlation/search even when the plaintext is also captured).
    assert rec["target"]["message_sha256"] == text_hash("analyze this repo")


def test_record_chat_per_call_override(tmp_path):
    log = tmp_path / "audit.jsonl"
    # Logger default is metadata-only; one call opts into verbatim capture.
    logger = AuditLogger(log, enabled=True, capture_content=False)
    rec = logger.record_chat(
        "scan", end_user="mattermost:carol", message="hello", capture_content=True,
    )
    assert rec["cui"] is True
    assert rec["target"]["message"] == "hello"

    # And the reverse: a capture_content=True logger can still be told "not this time".
    logger2 = AuditLogger(log, enabled=True, capture_content=True)
    rec2 = logger2.record_chat(
        "scan", end_user="mattermost:carol", message="secret stuff", capture_content=False,
    )
    assert rec2["cui"] is False
    assert "message" not in rec2["target"]


def test_record_chat_empty_message_has_no_hash(tmp_path):
    log = tmp_path / "audit.jsonl"
    logger = AuditLogger(log, enabled=True)
    rec = logger.record_chat("scan", end_user="mattermost:dave")
    assert rec["target"]["message_sha256"] == ""
    assert rec["target"]["reply_sha256"] == ""


def test_record_chat_chain_still_verifies(tmp_path):
    log = tmp_path / "audit.jsonl"
    logger = AuditLogger(log, enabled=True)
    logger.record("index", target={"repo": "r"})
    logger.record_chat("review_mr", end_user="mattermost:alice", message="hi", reply="hey")
    logger.record_chat(
        "scan", end_user="mattermost:bob", message="go", capture_content=True,
    )
    ok, msg = verify_chain(log)
    assert ok, msg


def test_audit_event_end_user_and_cui_roundtrip(tmp_path):
    """AuditEvent (the dataclass path via .event()) also carries end_user/cui."""
    log = tmp_path / "audit.jsonl"
    logger = AuditLogger(log, enabled=True)
    ev = AuditEvent(
        action="chat_message", target={"channel": "c1"}, end_user="mattermost:eve", cui=True,
    )
    rec = logger.event(ev)
    assert rec["end_user"] == "mattermost:eve"
    assert rec["cui"] is True
