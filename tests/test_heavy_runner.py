"""The heavy-analysis container runner (heavy.py) — graceful fallback + report parsing.

The .NET analyzer itself is validated by building/running the image; here we cover the
Python orchestration with a mocked container invocation.
"""

from __future__ import annotations

import json
import subprocess

from secagent.affordances import heavy
from secagent.config import Settings

_REPORT = {
    "schema": "secagent-analysis/v1", "language": "C#", "backend": "roslyn-msbuild",
    "functions": [{"name": "F", "qualified_name": "N.F", "signature": "", "file": "a.cs",
                   "line": 1, "kind": "method", "owning_type": "N"}],
    "types": [], "calls": [], "build": {"restored": True},
}


def _proc(stdout: str = "", returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def test_runner_success(tmp_path, monkeypatch):
    (tmp_path / "App.csproj").write_text("<Project/>")
    monkeypatch.setattr(heavy, "image_available", lambda r, i: True)
    monkeypatch.setattr(heavy.subprocess, "run", lambda *a, **k: _proc(json.dumps(_REPORT)))
    report = heavy.run_dotnet_analyzer(tmp_path, Settings())
    assert report is not None
    assert report.backend == "roslyn-msbuild"
    assert report.functions[0].qualified_name == "N.F"


def test_runner_falls_back_when_image_absent(tmp_path, monkeypatch):
    (tmp_path / "App.csproj").write_text("<Project/>")
    monkeypatch.setattr(heavy, "image_available", lambda r, i: False)
    assert heavy.run_dotnet_analyzer(tmp_path, Settings()) is None


def test_runner_falls_back_when_no_project(tmp_path, monkeypatch):
    monkeypatch.setattr(heavy, "image_available", lambda r, i: True)
    assert heavy.run_dotnet_analyzer(tmp_path, Settings()) is None


def test_runner_falls_back_on_bad_output(tmp_path, monkeypatch):
    (tmp_path / "App.csproj").write_text("<Project/>")
    monkeypatch.setattr(heavy, "image_available", lambda r, i: True)
    monkeypatch.setattr(heavy.subprocess, "run", lambda *a, **k: _proc("not json"))
    assert heavy.run_dotnet_analyzer(tmp_path, Settings()) is None


def test_runner_falls_back_on_nonzero_exit(tmp_path, monkeypatch):
    (tmp_path / "App.csproj").write_text("<Project/>")
    monkeypatch.setattr(heavy, "image_available", lambda r, i: True)
    monkeypatch.setattr(heavy.subprocess, "run", lambda *a, **k: _proc(returncode=3, stderr="boom"))
    assert heavy.run_dotnet_analyzer(tmp_path, Settings()) is None


_RUST_REPORT = {
    "schema": "secagent-analysis/v1", "language": "Rust", "backend": "rust-analyzer-scip",
    "functions": [{"name": "run", "qualified_name": "mycrate::Server::run", "signature": "",
                   "file": "src/lib.rs", "line": 1, "kind": "method",
                   "owning_type": "mycrate::Server"}],
    "types": [], "calls": [], "build": {"restored": True},
}


def test_rust_runner_success(tmp_path, monkeypatch):
    (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\n")
    monkeypatch.setattr(heavy, "image_available", lambda r, i: True)
    monkeypatch.setattr(heavy.subprocess, "run", lambda *a, **k: _proc(json.dumps(_RUST_REPORT)))
    report = heavy.run_rust_analyzer(tmp_path, Settings())
    assert report is not None
    assert report.backend == "rust-analyzer-scip"
    assert report.functions[0].qualified_name == "mycrate::Server::run"


def test_rust_runner_falls_back_when_no_cargo(tmp_path, monkeypatch):
    monkeypatch.setattr(heavy, "image_available", lambda r, i: True)
    assert heavy.run_rust_analyzer(tmp_path, Settings()) is None


def test_run_heavy_dispatches_to_rust_for_cargo(tmp_path, monkeypatch):
    (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\n")
    monkeypatch.setattr(heavy, "image_available", lambda r, i: True)
    monkeypatch.setattr(heavy.subprocess, "run", lambda *a, **k: _proc(json.dumps(_RUST_REPORT)))
    report = heavy.run_heavy_analyzer(tmp_path, Settings())
    assert report is not None and report.language == "Rust"


def test_run_heavy_dispatches_to_csharp_for_csproj(tmp_path, monkeypatch):
    (tmp_path / "App.csproj").write_text("<Project/>")
    monkeypatch.setattr(heavy, "image_available", lambda r, i: True)
    monkeypatch.setattr(heavy.subprocess, "run", lambda *a, **k: _proc(json.dumps(_REPORT)))
    report = heavy.run_heavy_analyzer(tmp_path, Settings())
    assert report is not None and report.language == "C#"


def test_run_heavy_none_when_no_project(tmp_path, monkeypatch):
    monkeypatch.setattr(heavy, "image_available", lambda r, i: True)
    assert heavy.run_heavy_analyzer(tmp_path, Settings()) is None
