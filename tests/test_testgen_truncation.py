"""Tests generated from part of a file must say so.

`testgen.max_file_bytes` (24000) silently capped what the generator saw. Click's
`core.py` is 147,588 characters, so generation stopped at line 636 — before `Command`,
`Group`, `Option`, `Argument` and `Parameter` even begin. The resulting file was written
as the unit test *for core.py*, indistinguishable from one produced with the whole file
in view.

That is the same defect `secagent scan` was fixed for, with a sharper edge: the output here
is runnable code. A reviewer reading a green suite has no way to tell that its silence
about `Group` means "never looked" rather than "nothing to test", and anything the model
does assert about the unseen part is invention.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from secagent.agents.testgen.agent import generate_tests
from secagent.config import Settings

from .conftest import make_chat_response, mock_client


def _settings(tmp_path, cap) -> Settings:
    s = Settings()
    s.affordances.llm_summaries = False
    s.affordances.store_dir = str(tmp_path / "store")
    s.testgen.max_file_bytes = cap
    s.testgen.max_unit_files = 5
    s.testgen.max_functional_components = 0
    return s


def _repo(tmp_path, size):
    repo = tmp_path / "repo"
    repo.mkdir()
    body = "def visible():\n    return 1\n"
    filler = "\n".join(f"def pad{i}():\n    return {i}" for i in range(size))
    (repo / "big.py").write_text(body + filler)
    return repo


def _llm():
    return mock_client(lambda r: httpx.Response(
        200, json=make_chat_response(content="def test_x():\n    assert True\n")))


def test_truncated_generation_is_flagged_everywhere(tmp_path):
    repo = _repo(tmp_path, 400)
    out = tmp_path / "out"
    result = generate_tests(repo, _settings(tmp_path, 200), out_dir=out,
                            functional=False, llm=_llm())

    assert result["partial_count"] >= 1
    assert "big.py" in result["partial"]

    manifest = json.loads((out / "manifest.json").read_text())
    entry = next(t for t in manifest["tests"] if t["target"] == "big.py")
    assert entry["saw_chars"] < entry["total_chars"]

    readme = (out / "README.md").read_text()
    assert "Incomplete source coverage" in readme
    assert "big.py" in readme

    generated = next(p for p in Path(out).rglob("*.py"))
    head = generated.read_text()
    assert "WARNING" in head and "NOT seen" in head, \
        "the warning must survive into the file a developer actually opens"


def test_untruncated_generation_says_nothing_about_truncation(tmp_path):
    """Paired silence test: a notice that always fires is one nobody reads."""
    repo = _repo(tmp_path, 3)
    out = tmp_path / "out"
    result = generate_tests(repo, _settings(tmp_path, 100_000), out_dir=out,
                            functional=False, llm=_llm())

    assert result["partial_count"] == 0
    assert result["partial"] == {}
    assert "Incomplete source coverage" not in (out / "README.md").read_text()
    for p in Path(out).rglob("*.py"):
        assert "WARNING" not in p.read_text()


def test_an_empty_generation_is_not_dressed_up_as_a_test(tmp_path):
    """Prepending the truncation banner before checking emptiness made an empty
    generation look successful: the file was non-empty, so `ok` was true and the manifest
    reported a test whose entire content was a warning."""
    repo = _repo(tmp_path, 400)
    out = tmp_path / "out"
    empty = mock_client(lambda r: httpx.Response(200, json=make_chat_response(content="")))
    result = generate_tests(repo, _settings(tmp_path, 200), out_dir=out,
                            functional=False, llm=empty)

    assert result["unit_count"] == 0
    manifest = json.loads((out / "manifest.json").read_text())
    assert all(not t["ok"] for t in manifest["tests"])
    assert not list(Path(out).rglob("*.py")), "wrote a file containing only a banner"
