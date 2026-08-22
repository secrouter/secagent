"""Tests for the pi guard (piguard.py) — the headless-run failure detectors.

Covers the pure parts only: headless detection, the models.json contextWindow
preflight clamp, the session-transcript post-mortem, and the worktree snapshot/diff.
The `pi run` wiring exercises these through an injected exec_fn and is deliberately
not re-driven here — launch assembly already has its own tests (test_leanctx.py /
test_kg_config_launch.py).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from secagent import piguard

# ── is_headless ───────────────────────────────────────────────────────────────────


def test_is_headless_detects_print_flags():
    assert piguard.is_headless(["-p", "fix the bug"]) is True
    assert piguard.is_headless(["--print", "fix the bug"]) is True
    assert piguard.is_headless(["--mode", "rpc"]) is False
    assert piguard.is_headless([]) is False
    # No prefix matching: a different flag that merely starts alike is not headless.
    assert piguard.is_headless(["--printer"]) is False


# ── preflight_context ─────────────────────────────────────────────────────────────


def _write_models_json(agent_dir: Path, models: list[dict]) -> Path:
    path = agent_dir / "models.json"
    agent_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "providers": {
            "secrouter": {
                "api": "openai-completions",
                "baseUrl": "http://localhost:8000/v1",
                "apiKey": "!secagent token --user",
                "models": models,
            }
        }
    }, indent=2) + "\n", encoding="utf-8")
    return path


class _Resp:
    """Minimal stand-in for httpx.Response — just what _served_windows touches."""

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self):
        return self._payload


def test_preflight_clamps_oversized_window_and_writes_back(tmp_path, monkeypatch):
    path = _write_models_json(tmp_path, [
        {"id": "gemma-3-12b-it", "contextWindow": 65536},
        {"id": "small", "contextWindow": 8192},
    ])
    seen: list[str] = []

    def fake_get(url, timeout):
        seen.append(url)
        return _Resp({"object": "list", "data": [
            {"id": "gemma-3-12b-it", "object": "model", "max_model_len": 32768},
            {"id": "small", "object": "model", "max_model_len": 16384},
        ]})

    monkeypatch.setattr(piguard.httpx, "get", fake_get)
    notes = piguard.preflight_context(tmp_path)

    # baseUrl already ends in /v1 — the probe must not double it.
    assert seen == ["http://localhost:8000/v1/models"]
    assert notes == ["gemma-3-12b-it: contextWindow 65536 -> 32768 (served max_model_len)"]
    written = json.loads(path.read_text())
    models = written["providers"]["secrouter"]["models"]
    assert models[0]["contextWindow"] == 32768   # clamped down to the served ceiling
    assert models[1]["contextWindow"] == 8192    # smaller than served: a valid choice, untouched


def test_preflight_no_op_when_windows_fit(tmp_path, monkeypatch):
    path = _write_models_json(tmp_path, [{"id": "gemma", "contextWindow": 32768}])
    before = path.read_text()
    monkeypatch.setattr(
        piguard.httpx, "get",
        lambda url, timeout: _Resp({"data": [{"id": "gemma", "max_model_len": 32768}]}))
    assert piguard.preflight_context(tmp_path) == []
    assert path.read_text() == before  # nothing to clamp = file left byte-identical


def test_preflight_skips_models_the_server_does_not_report(tmp_path, monkeypatch):
    # A non-vLLM server omits max_model_len entirely — no evidence, no clamp.
    path = _write_models_json(tmp_path, [{"id": "gemma", "contextWindow": 999999}])
    monkeypatch.setattr(
        piguard.httpx, "get", lambda url, timeout: _Resp({"data": [{"id": "gemma"}]}))
    assert piguard.preflight_context(tmp_path) == []
    assert json.loads(path.read_text())["providers"]["secrouter"]["models"][0][
        "contextWindow"] == 999999


def test_preflight_survives_network_error(tmp_path, monkeypatch):
    path = _write_models_json(tmp_path, [{"id": "gemma", "contextWindow": 65536}])
    before = path.read_text()

    def boom(url, timeout):
        raise piguard.httpx.ConnectError("connection refused")

    monkeypatch.setattr(piguard.httpx, "get", boom)
    assert piguard.preflight_context(tmp_path) == []  # never blocks a launch
    assert path.read_text() == before


def test_preflight_missing_or_malformed_models_json(tmp_path, monkeypatch):
    monkeypatch.setattr(piguard.httpx, "get", lambda url, timeout: _Resp({}))
    assert piguard.preflight_context(tmp_path / "nowhere") == []
    bad = tmp_path / "agent"
    bad.mkdir()
    (bad / "models.json").write_text("not json {")
    assert piguard.preflight_context(bad) == []


# ── session_verdict ───────────────────────────────────────────────────────────────


def _write_session(agent_dir: Path, last_record: dict, *, raw_last: str | None = None) -> Path:
    session_dir = agent_dir / "sessions" / "-Users-dev-project"
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_dir / "20260822T120000_abc123.jsonl"
    first = json.dumps({"type": "session", "cwd": "/Users/dev/project"})
    last = raw_last if raw_last is not None else json.dumps(last_record)
    path.write_text(first + "\n" + last + "\n", encoding="utf-8")
    return path


def test_session_verdict_flags_length_stop(tmp_path):
    since = time.time() - 60
    path = _write_session(tmp_path, {
        "type": "message",
        "message": {"role": "assistant", "content": [],
                    "stopReason": "length",
                    "usage": {"input": 28747, "output": 0}},
    })
    verdict = piguard.session_verdict(tmp_path, Path("/Users/dev/project"), since)
    assert verdict is not None
    assert path.name in verdict
    assert "stopReason=length" in verdict
    assert "input=28747 tokens" in verdict
    assert "context exhaustion" in verdict


def test_session_verdict_flags_empty_content_even_without_length(tmp_path):
    _write_session(tmp_path, {
        "message": {"role": "assistant", "content": [{"type": "text", "text": "   "}],
                    "stopReason": "stop"},
    })
    verdict = piguard.session_verdict(tmp_path, Path("/x"), time.time() - 60)
    assert verdict is not None
    assert "final message empty" in verdict


def test_session_verdict_healthy_session_is_none(tmp_path):
    _write_session(tmp_path, {
        "message": {"role": "assistant",
                    "content": [{"type": "text", "text": "Done — patched two files."}],
                    "stopReason": "stop", "usage": {"input": 1200, "output": 340}},
    })
    assert piguard.session_verdict(tmp_path, Path("/x"), time.time() - 60) is None


def test_session_verdict_malformed_and_missing_are_none(tmp_path):
    # No sessions directory at all.
    assert piguard.session_verdict(tmp_path, Path("/x"), 0.0) is None
    # Last line is not JSON.
    _write_session(tmp_path, {}, raw_last="{truncated garba")
    assert piguard.session_verdict(tmp_path, Path("/x"), time.time() - 60) is None


def test_session_verdict_ignores_transcripts_older_than_the_run(tmp_path):
    path = _write_session(tmp_path, {
        "message": {"role": "assistant", "content": [], "stopReason": "length"},
    })
    old = time.time() - 3600
    os.utime(path, (old, old))  # a PREVIOUS run's dead session must not indict this one
    assert piguard.session_verdict(tmp_path, Path("/x"), time.time() - 60) is None


# ── snapshot / diff ───────────────────────────────────────────────────────────────


def test_snapshot_and_diff_report_added_modified_removed(tmp_path):
    (tmp_path / "keep.py").write_text("unchanged")
    (tmp_path / "edit.py").write_text("v1")
    (tmp_path / "gone.py").write_text("doomed")
    before = piguard.snapshot_worktree(tmp_path)

    (tmp_path / "edit.py").write_text("v2 — longer")
    (tmp_path / "gone.py").unlink()
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "new.py").write_text("born")
    after = piguard.snapshot_worktree(tmp_path)

    assert piguard.diff_worktree(before, after) == [
        f"added: {os.path.join('sub', 'new.py')}",
        "modified: edit.py",
        "removed: gone.py",
    ]


def test_snapshot_skips_git_and_secagent_dirs(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "index").write_text("churn")
    (tmp_path / ".secagent").mkdir()
    (tmp_path / ".secagent" / "index.db").write_text("store")
    (tmp_path / "real.py").write_text("code")
    assert set(piguard.snapshot_worktree(tmp_path)) == {"real.py"}


def test_diff_worktree_empty_when_nothing_changed(tmp_path):
    (tmp_path / "a.py").write_text("stable")
    snap = piguard.snapshot_worktree(tmp_path)
    assert piguard.diff_worktree(snap, piguard.snapshot_worktree(tmp_path)) == []
    assert piguard.diff_worktree(snap, dict(snap)) == []
