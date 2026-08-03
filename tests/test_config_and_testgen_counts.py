"""Config that does nothing must not look like config that worked, and counts must
count what happened.

Both found by an evaluation agent on UC5.

A misspelled top-level section in a `--config` YAML was silently ignored, so the run
proceeded on defaults while the user believed their settings applied — and then drew
conclusions from output produced under a configuration that never existed.

`unit_count`/`functional_count` counted every file the generator *attempted*, including
the ones where the model returned nothing and no file was written. A run that produced
zero usable tests could report "12 unit tests".
"""

from __future__ import annotations

import httpx
import pytest

from secagent.config import Settings, load_settings

from .conftest import make_chat_response, mock_client


def test_unknown_config_section_is_rejected(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("test_gen:\n  max_unit_files: 3\n")     # should be `testgen`
    with pytest.raises(ValueError, match="unknown config section"):
        load_settings(cfg)


def test_the_error_names_the_valid_sections(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("nonsense: 1\n")
    with pytest.raises(ValueError, match="testgen"):
        load_settings(cfg)


def test_a_correct_config_still_loads(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("testgen:\n  max_unit_files: 3\n")
    assert load_settings(cfg).testgen.max_unit_files == 3


# --- counts -------------------------------------------------------------------

def _settings(tmp_path) -> Settings:
    s = Settings()
    s.affordances.llm_summaries = False
    s.affordances.store_dir = str(tmp_path / "store")
    s.testgen.max_unit_files = 5
    s.testgen.max_functional_components = 0
    return s


def test_counts_report_written_tests_not_attempts(tmp_path):
    from secagent.agents.testgen.agent import generate_tests

    repo = tmp_path / "repo"
    repo.mkdir()
    for i in range(3):
        (repo / f"m{i}.py").write_text(f"def f{i}():\n    return {i}\n")

    # The model returns nothing usable for every file.
    llm = mock_client(lambda r: httpx.Response(200, json=make_chat_response(content="")))
    result = generate_tests(repo, _settings(tmp_path), out_dir=tmp_path / "o",
                            functional=False, llm=llm)

    assert result["unit_count"] == 0, "reported tests that were never written"
    assert result["attempted"] >= 1
    assert result["failed"] == result["attempted"]


def test_counts_are_right_when_generation_succeeds(tmp_path):
    from secagent.agents.testgen.agent import generate_tests

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "m.py").write_text("def f():\n    return 1\n")
    llm = mock_client(lambda r: httpx.Response(
        200, json=make_chat_response(content="def test_f():\n    assert True\n")))
    result = generate_tests(repo, _settings(tmp_path), out_dir=tmp_path / "o",
                            functional=False, llm=llm)
    assert result["unit_count"] == 1 and result["failed"] == 0


def test_library_code_is_generated_before_examples(tmp_path):
    """A bounded run spent its whole budget on `examples/*` because file_records() is
    ordered by path. Alphabetical order is not a priority order.

    This asserted `sorted(["examples/demo.py", "src/click/core.py"], key=path_rank)[0]`
    — a test of `path_rank`, not of testgen's use of it. It could not fail if
    `_gen_unit` stopped ranking altogether. The behavioural version lives in
    `tests/test_priority.py::test_testgen_generates_for_library_files_before_examples`;
    this one keeps the local claim honest by making the budget actually bind here.
    """
    from secagent.agents.testgen.agent import generate_tests

    repo = tmp_path / "repo"
    (repo / "examples").mkdir(parents=True)
    (repo / "zlib").mkdir(parents=True)
    (repo / "examples" / "a_demo.py").write_text("def demo():\n    return 1\n")
    (repo / "zlib" / "z_core.py").write_text("def core():\n    return 2\n")

    s = _settings(tmp_path)
    s.testgen.max_unit_files = 1
    llm = mock_client(lambda r: httpx.Response(
        200, json=make_chat_response(content="def test_x():\n    assert True\n")))
    result = generate_tests(repo, s, out_dir=tmp_path / "o", functional=False, llm=llm)

    assert result["attempted"] == 1, "the cap must bind or this proves nothing"
    assert [g["target"] for g in result["generated"]] == ["zlib/z_core.py"]


# --- concurrency must not change the result -----------------------------------

def test_parallel_generation_produces_the_same_set(tmp_path):
    """Throughput is only worth having if the output is identical."""
    from secagent.agents.testgen.agent import generate_tests

    repo = tmp_path / "repo"
    repo.mkdir()
    for i in range(6):
        (repo / f"m{i}.py").write_text(f"def f{i}():\n    return {i}\n")

    def run(workers, out):
        s = _settings(tmp_path)
        s.testgen.workers = workers
        s.testgen.max_unit_files = 6
        llm = mock_client(lambda r: httpx.Response(
            200, json=make_chat_response(content="def test_x():\n    assert True\n")))
        return generate_tests(repo, s, out_dir=out, functional=False, llm=llm)

    serial = run(1, tmp_path / "a")
    parallel = run(4, tmp_path / "b")

    assert serial["unit_count"] == parallel["unit_count"] == 6
    assert {t["target"] for t in serial["generated"]} == \
           {t["target"] for t in parallel["generated"]}


def test_a_generation_failure_does_not_kill_the_run(tmp_path):
    """One file that fails must cost that file, not the whole run — and must be visible
    rather than returning an empty string that reads as 'nothing to write'."""
    from secagent.agents.testgen.agent import generate_tests

    repo = tmp_path / "repo"
    repo.mkdir()
    for i in range(3):
        (repo / f"m{i}.py").write_text(f"def f{i}():\n    return {i}\n")

    def flaky(request):
        # Fail deterministically for one file, whatever the retry count — keying on a
        # call counter made the outcome depend on the client's retries, not on the
        # behaviour under test.
        if b"m0.py" in request.content:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, json=make_chat_response(
            content="def test_x():\n    assert True\n"))

    s = _settings(tmp_path)
    s.testgen.workers = 1
    result = generate_tests(repo, s, out_dir=tmp_path / "o", functional=False,
                            llm=mock_client(flaky))

    assert result["attempted"] == 3
    assert result["failed"] >= 1 and result["unit_count"] >= 1
