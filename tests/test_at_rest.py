"""Tests for at-rest hardening and secure purge (CMMC-2)."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from secagent.affordances.api import index_repo, purge_store
from secagent.config import Settings
from secagent.security import harden_path, secure_delete_file

FIXTURE = Path(__file__).parent / "fixtures" / "sample_repo"


def _settings(tmp_path) -> Settings:
    s = Settings()
    s.affordances.llm_summaries = False
    s.affordances.store_dir = str(tmp_path / "store")
    return s


def test_secure_delete_removes_file(tmp_path):
    f = tmp_path / "secret.txt"
    f.write_text("classified")
    secure_delete_file(f)
    assert not f.exists()


def test_harden_path_sets_mode(tmp_path):
    f = tmp_path / "f"
    f.write_text("x")
    assert harden_path(f, 0o600)
    assert stat.S_IMODE(os.stat(f).st_mode) == 0o600


def test_store_dir_is_owner_only(tmp_path):
    s = _settings(tmp_path)
    index_repo(FIXTURE, s)
    store_dir = Path(s.affordances.store_dir)
    mode = stat.S_IMODE(os.stat(store_dir).st_mode)
    assert mode == 0o700
    db_mode = stat.S_IMODE(os.stat(store_dir / "index.db").st_mode)
    assert db_mode == 0o600


def test_purge_removes_store(tmp_path):
    s = _settings(tmp_path)
    index_repo(FIXTURE, s)
    store_dir = Path(s.affordances.store_dir)
    assert store_dir.exists()
    report = purge_store(FIXTURE, s)
    assert report["existed"] is True
    assert report["removed_files"] > 0
    assert not store_dir.exists()


def test_purge_noop_when_absent(tmp_path):
    s = _settings(tmp_path)
    report = purge_store(FIXTURE, s)
    assert report["existed"] is False
    assert report["removed_files"] == 0


def test_audit_log_is_owner_only(tmp_path):
    s = _settings(tmp_path)
    s.audit.enabled = True
    s.audit.path = str(tmp_path / "audit" / "audit.jsonl")
    index_repo(FIXTURE, s)
    mode = stat.S_IMODE(os.stat(s.audit.path).st_mode)
    assert mode == 0o600
