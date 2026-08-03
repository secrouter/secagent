"""A scanner must never report an all-clear it did not earn.

Found by devE (run 3): `secagent scan` printed `"total": 0, findings: []` and "No findings
against the configured rules" AFTER six consecutive LLM calls had failed — the failures
appeared only on stderr. On a memory-safety tool a silent false all-clear is the most
dangerous output possible: it is indistinguishable from "this code is safe".
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from secagent.agents.scan.agent import scan_repo
from secagent.agents.scan.report import render_markdown
from secagent.config import Settings

from .conftest import make_chat_response, mock_client

REPO_ROOT = Path(__file__).resolve().parents[1]
RULES = REPO_ROOT / "config" / "rules" / "embedded-cpp.yaml"
FIXTURE = Path(__file__).parent / "fixtures" / "cpp_repo"


def _settings(tmp_path) -> Settings:
    s = Settings()
    s.affordances.llm_summaries = False
    s.affordances.store_dir = str(tmp_path / "store")
    s.scan.rules_profile = str(RULES)
    return s


def test_empty_model_output_is_reported_as_a_failure(tmp_path):
    """The exact observed case: the model returns nothing, so nothing was analyzed."""
    llm = mock_client(lambda r: httpx.Response(200, json=make_chat_response(content="")))
    result = scan_repo(FIXTURE, _settings(tmp_path), out_dir=tmp_path / "o", llm=llm)

    assert result["files_failed"] >= 1
    assert result["analysis_complete"] is False

    data = json.loads(Path(result["report_json"]).read_text())
    assert data["failures"], "scan.json must name the files it could not analyze"
    assert "empty content" in next(iter(data["failures"].values()))

    md = Path(result["report_md"]).read_text()
    assert "INCOMPLETE SCAN" in md
    assert "does **not** mean those files are clean" in md
    assert "No findings against the configured rules." not in md   # the false all-clear


def test_llm_exception_is_reported_as_a_failure(tmp_path):
    def boom(request):
        raise httpx.ConnectError("endpoint down")

    llm = mock_client(boom)
    result = scan_repo(FIXTURE, _settings(tmp_path), out_dir=tmp_path / "o", llm=llm)
    assert result["analysis_complete"] is False
    data = json.loads(Path(result["report_json"]).read_text())
    assert "LLM call failed" in next(iter(data["failures"].values()))


def test_non_json_output_is_reported_as_a_failure(tmp_path):
    llm = mock_client(lambda r: httpx.Response(200, json=make_chat_response(
        content="I am afraid I cannot help with that.")))
    result = scan_repo(FIXTURE, _settings(tmp_path), out_dir=tmp_path / "o", llm=llm)
    assert result["analysis_complete"] is False
    data = json.loads(Path(result["report_json"]).read_text())
    assert "no JSON array" in next(iter(data["failures"].values()))


def test_a_genuinely_clean_scan_still_reads_as_clean(tmp_path):
    """The other half: a real all-clear must NOT be muddied by scary warnings."""
    llm = mock_client(lambda r: httpx.Response(200, json=make_chat_response(content="[]")))
    result = scan_repo(FIXTURE, _settings(tmp_path), out_dir=tmp_path / "o", llm=llm)

    assert result["files_failed"] == 0
    assert result["analysis_complete"] is True
    md = Path(result["report_md"]).read_text()
    assert "No findings against the configured rules." in md
    assert "INCOMPLETE" not in md


def test_markdown_banner_is_driven_by_the_summary():
    md = render_markdown([], project="p", ruleset_name="r",
                         summary={"total": 0, "files_failed": 3})
    assert "INCOMPLETE SCAN — 3 file(s)" in md


def test_refresh_summaries_bypasses_the_incremental_skip(tmp_path, monkeypatch):
    """devG (run 4): `--refresh-summaries` is documented to "regenerate LLM summaries even
    if cached", but the SHA skip short-circuits before _process_file — the only place the
    flag has any effect — so on an already-indexed repo it silently did nothing."""
    from secagent.affordances.api import index_repo

    repo = tmp_path / "r"
    repo.mkdir()
    (repo / "a.py").write_text("def f():\n    return 1\n")

    s = Settings()
    s.affordances.llm_summaries = False
    s.affordances.store_dir = str(tmp_path / "store")

    first = index_repo(repo, s)
    assert first["updated"] == 1 and first["skipped"] == 0

    # Unchanged file: the incremental skip is correct here.
    again = index_repo(repo, s)
    assert again["skipped"] == 1 and again["updated"] == 0

    # ...but --refresh-summaries must force the file through summarization again.
    s.affordances.refresh_summaries = True
    forced = index_repo(repo, s)
    assert forced["updated"] == 1, "refresh_summaries must bypass the SHA skip"
    assert forced["skipped"] == 0


# --- partial coverage ---------------------------------------------------------
# Truncating a file to `scan.max_file_bytes` and then reporting it as *analyzed* is the
# same false all-clear wearing a different hat: the model never saw the rest, but the
# report reads exactly like a clean file. It became urgent once lowering max_file_bytes
# turned into the recommended workaround for a model that cannot reason over a large file.

def test_truncated_file_is_flagged_as_partial(tmp_path):
    llm = mock_client(lambda r: httpx.Response(200, json=make_chat_response(content="[]")))
    s = _settings(tmp_path)
    s.scan.max_file_bytes = 120          # forces truncation of the fixture sources
    result = scan_repo(FIXTURE, s, out_dir=tmp_path / "o", llm=llm)

    data = json.loads(Path(result["report_json"]).read_text())
    assert data["summary"]["files_partial"] >= 1
    assert data["partial"], "the report must name which files were cut short"
    why = next(iter(data["partial"].values()))
    assert "never read" in why and "120" in why

    md = Path(result["report_md"]).read_text()
    assert "PARTIAL COVERAGE" in md
    assert "No findings against the configured rules." not in md, \
        "a truncated scan must not read as a clean one"


def test_untruncated_scan_says_nothing_about_partial_coverage(tmp_path):
    """The paired silence test. A first attempt compared stat() BYTES against len()
    CHARACTERS, so a single non-ASCII character marked a complete scan as partial —
    a warning that always fires is one nobody reads."""
    src = tmp_path / "repo"
    src.mkdir()
    # Multi-byte characters: byte length and character length genuinely differ here.
    (src / "a.c").write_text("/* café — naïve ✓ */\nint main(void) { return 0; }\n")

    llm = mock_client(lambda r: httpx.Response(200, json=make_chat_response(content="[]")))
    s = _settings(tmp_path)
    s.scan.max_file_bytes = 40000
    result = scan_repo(src, s, out_dir=tmp_path / "o", llm=llm)

    data = json.loads(Path(result["report_json"]).read_text())
    assert data["summary"]["files_partial"] == 0
    assert data["partial"] == {}
    assert "PARTIAL COVERAGE" not in Path(result["report_md"]).read_text()


# --- cost visibility ----------------------------------------------------------

def test_uncapped_scan_of_a_large_repo_says_what_it_will_cost(tmp_path, caplog):
    """Scanning everything is the default, because a scanner that quietly stops at file
    50 reports an all-clear it never earned. But one model call per file runs minutes
    each locally, so the cost is stated BEFORE it is spent."""
    from secagent.agents.scan.agent import _COST_NOTICE_FILES

    repo = tmp_path / "repo"
    repo.mkdir()
    for i in range(_COST_NOTICE_FILES + 5):
        (repo / f"m{i}.c").write_text(f"int f{i}(void) {{ return {i}; }}\n")

    llm = mock_client(lambda r: httpx.Response(200, json=make_chat_response(content="[]")))
    s = _settings(tmp_path)
    s.scan.max_files = 0
    with caplog.at_level("WARNING"):
        scan_repo(repo, s, out_dir=tmp_path / "o", llm=llm)
    assert "--max-files" in caplog.text, "must name the escape hatch"


def test_no_cost_notice_on_a_small_repo(tmp_path, caplog):
    """Paired silence test: a notice that always fires is one nobody reads."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.c").write_text("int main(void) { return 0; }\n")

    llm = mock_client(lambda r: httpx.Response(200, json=make_chat_response(content="[]")))
    s = _settings(tmp_path)
    s.scan.max_files = 0
    with caplog.at_level("WARNING"):
        scan_repo(repo, s, out_dir=tmp_path / "o", llm=llm)
    assert "--max-files" not in caplog.text


def test_no_cost_notice_when_a_cap_is_set(tmp_path, caplog):
    repo = tmp_path / "repo"
    repo.mkdir()
    for i in range(30):
        (repo / f"m{i}.c").write_text(f"int f{i}(void) {{ return {i}; }}\n")

    llm = mock_client(lambda r: httpx.Response(200, json=make_chat_response(content="[]")))
    s = _settings(tmp_path)
    s.scan.max_files = 3
    with caplog.at_level("WARNING"):
        scan_repo(repo, s, out_dir=tmp_path / "o", llm=llm)
    assert "--max-files" not in caplog.text


# --- bounded scans must say what they skipped ---------------------------------

def test_capped_scan_reports_the_files_it_never_looked_at(tmp_path, caplog):
    """`--max-files` made a bounded scan and an exhaustive one produce identical-looking
    "no findings" reports. On a safety scanner that difference is the whole point."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for i in range(10):
        (repo / f"m{i}.c").write_text(f"int f{i}(void) {{ return {i}; }}\n")

    llm = mock_client(lambda r: httpx.Response(200, json=make_chat_response(content="[]")))
    s = _settings(tmp_path)
    s.scan.max_files = 3
    with caplog.at_level("WARNING"):
        result = scan_repo(repo, s, out_dir=tmp_path / "o", llm=llm)

    data = json.loads(Path(result["report_json"]).read_text())
    assert data["summary"]["files_eligible"] == 10
    assert data["summary"]["files_skipped_by_cap"] == 7
    assert "BOUNDED SCAN" in Path(result["report_md"]).read_text()
    assert "lower bound" in caplog.text


def test_uncapped_scan_says_nothing_about_skipping(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for i in range(3):
        (repo / f"m{i}.c").write_text(f"int f{i}(void) {{ return {i}; }}\n")

    llm = mock_client(lambda r: httpx.Response(200, json=make_chat_response(content="[]")))
    s = _settings(tmp_path)
    s.scan.max_files = 0
    result = scan_repo(repo, s, out_dir=tmp_path / "o", llm=llm)

    data = json.loads(Path(result["report_json"]).read_text())
    assert data["summary"]["files_skipped_by_cap"] == 0
    md = Path(result["report_md"]).read_text()
    assert "BOUNDED SCAN" not in md
    assert "No findings against the configured rules." in md


def test_explicit_paths_scan_only_those_files(tmp_path):
    """scan_repo already accepted `paths`; it was simply never reachable from the CLI."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for i in range(5):
        (repo / f"m{i}.c").write_text(f"int f{i}(void) {{ return {i}; }}\n")

    llm = mock_client(lambda r: httpx.Response(200, json=make_chat_response(content="[]")))
    s = _settings(tmp_path)
    result = scan_repo(repo, s, out_dir=tmp_path / "o", llm=llm, paths=["m1.c", "m3.c"])
    assert result["files_scanned"] == 2


# --- an unreadable file is a failure, not a cap ------------------------------

def test_unreadable_file_is_reported_as_a_failure_not_a_cap(tmp_path, caplog):
    """A file selected for scanning that cannot be read used to vanish without trace.
    A first fix counted it — but against `scan.max_files`, so an UNCAPPED run reported
    files "skipped by scan.max_files=0", naming a setting that had excluded nothing.
    A wrong reason is worse than no reason: it sends you to fix what isn't broken."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "good.c").write_text("int main(void) { return 0; }\n")
    (repo / "bad.c").write_bytes(b"\xff\xfe\x00 int main(void){}\n")   # not UTF-8

    llm = mock_client(lambda r: httpx.Response(200, json=make_chat_response(content="[]")))
    s = _settings(tmp_path)
    s.scan.max_files = 0
    with caplog.at_level("WARNING"):
        result = scan_repo(repo, s, out_dir=tmp_path / "o", llm=llm)

    data = json.loads(Path(result["report_json"]).read_text())
    assert "bad.c" in data["failures"], "the unreadable file must be named"
    assert data["summary"]["files_skipped_by_cap"] == 0, \
        "nothing was excluded by a cap — do not blame one"
    assert result["analysis_complete"] is False
    assert "INCOMPLETE" in Path(result["report_md"]).read_text()


def test_a_bounded_run_is_not_reported_as_complete(tmp_path):
    """Deliberate incompleteness is still incompleteness: a consumer reading this flag
    to decide whether "no findings" means "clean" needs to know."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for i in range(5):
        (repo / f"m{i}.c").write_text(f"int f{i}(void) {{ return {i}; }}\n")

    llm = mock_client(lambda r: httpx.Response(200, json=make_chat_response(content="[]")))
    s = _settings(tmp_path)
    s.scan.max_files = 2
    assert scan_repo(repo, s, out_dir=tmp_path / "o", llm=llm)["analysis_complete"] is False


def test_a_full_clean_run_is_reported_as_complete(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.c").write_text("int main(void) { return 0; }\n")

    llm = mock_client(lambda r: httpx.Response(200, json=make_chat_response(content="[]")))
    s = _settings(tmp_path)
    s.scan.max_files = 0
    assert scan_repo(repo, s, out_dir=tmp_path / "o", llm=llm)["analysis_complete"] is True


def test_the_persona_follows_the_rule_profile(tmp_path):
    """The prompt said "C/C++ embedded-systems reviewer" even when scanning Rust against
    rust-safety.yaml — rules Rust-aware, framing telling the model to think in C. On the
    language whose whole safety argument differs from C's, that is not cosmetic."""
    from secagent.agents.scan.agent import _system_prompt
    from secagent.agents.scan.rules import load_rules

    rust = load_rules(REPO_ROOT / "config" / "rules" / "rust-safety.yaml")
    prompt = _system_prompt(rust)
    assert "Rust" in prompt
    assert "C/C++" not in prompt

    c_profile = load_rules(RULES)
    assert "C / C++" in _system_prompt(c_profile) or "C/C++" in _system_prompt(c_profile)


def test_a_file_that_never_converges_is_bounded_not_waited_out(tmp_path, caplog):
    """A 3894-byte header was measured consuming a 58988-token budget over 10.5 minutes
    and still producing nothing. Without a bound, one such file consumes an entire run —
    which is how two evaluation agents lost their timebox. On expiry the file is reported
    NOT ANALYZED, which is true and useful.

    The previous version of this test proved none of that. Its handler slept 30s and then
    raised `AssertionError`, which is not in `(httpx.HTTPError, LLMError)` — so it escaped
    the retry loop immediately and the file was reported NOT ANALYZED because the handler
    threw, at 30s per rule group, with the deadline never consulted. `httpx.MockTransport`
    does not enforce timeouts. Its `s.llm.max_retries = 1` did nothing either: the
    injected client carries its own `LLMConfig`, so scan's `s.llm` never reached it.

    This version raises `httpx.ReadTimeout` — what the real transport raises on expiry —
    against the client's own retry setting, and asserts the wall clock.
    """
    import time

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "slow.c").write_text("int main(void) { return 0; }\n")

    def hang(request):
        time.sleep(0.2)
        raise httpx.ReadTimeout("simulated transport timeout", request=request)

    llm = mock_client(hang, max_retries=3)
    s = _settings(tmp_path)
    s.scan.per_file_timeout_s = 0.5
    s.scan.workers = 1
    s.scan.rule_granularity = "all"  # one call per file, so the bound below is per-file
    started = time.monotonic()
    with caplog.at_level("WARNING"):
        result = scan_repo(repo, s, out_dir=tmp_path / "o", llm=llm)
    elapsed = time.monotonic() - started

    assert result["files_failed"] == 1
    assert result["analysis_complete"] is False
    data = json.loads(Path(result["report_json"]).read_text())
    assert "slow.c" in data["failures"], "the file must be named as NOT ANALYZED"
    assert "NOT ANALYZED" in caplog.text
    # The setting says "hard ceiling on wall-clock per file, INCLUDING the empty-content
    # retry". It was applied per HTTP request instead, so max_retries multiplied it and
    # the escalation could multiply it again: 300s admitted ~1800s. Unbounded, this file
    # takes ~3.6s (0.2 + 1s backoff + 0.2 + 2s backoff + 0.2).
    assert elapsed < 2 * s.scan.per_file_timeout_s, (
        f"per_file_timeout_s={s.scan.per_file_timeout_s} bounded the file at "
        f"{elapsed:.1f}s, which is more than the deadline it promises")


def test_every_file_is_still_scanned_when_running_in_parallel(tmp_path):
    """Concurrency must not lose files or findings."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for i in range(6):
        (repo / f"m{i}.c").write_text(f"int f{i}(void) {{ return {i}; }}\n")

    llm = mock_client(lambda r: httpx.Response(200, json=make_chat_response(
        content='[{"rule":"BUF-001","line":1,"severity":"high","message":"x"}]')))
    s = _settings(tmp_path)
    s.scan.workers = 4
    result = scan_repo(repo, s, out_dir=tmp_path / "o", llm=llm)

    assert result["files_scanned"] == 6
    assert result["files_failed"] == 0
    # One finding per file. The rule set is sent as several per-category calls, so the
    # same defect can come back more than once; identical (file, line, rule) findings
    # are one finding, not seven.
    assert result["summary"]["total"] == 6
    assert result["analysis_complete"] is True


# --- per-category rule splitting ---------------------------------------------

def test_a_partly_failing_file_names_the_rule_groups_it_missed(tmp_path):
    """32 rules in one call exhausted the budget and returned nothing. Split per
    category, three of seven groups still failed — but four succeeded, and saying WHICH
    went unexamined beats dropping the file."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.c").write_text("int main(void) { return 0; }\n")

    def partial(request):
        # The 'buffer' group fails; the rest answer.
        if b"BUF-001" in request.content:
            return httpx.Response(200, json=make_chat_response(content=""))
        return httpx.Response(200, json=make_chat_response(content="[]"))

    s = _settings(tmp_path)
    s.scan.split_rules_by_category = True
    result = scan_repo(repo, s, out_dir=tmp_path / "o", llm=mock_client(partial))

    data = json.loads(Path(result["report_json"]).read_text())
    # Partial coverage is its own state, not a failure — the groups that ran produced
    # real findings and must not be discarded with the ones that did not.
    why = data["partial"]["a.c"]
    assert "rule group(s) were not applied" in why
    assert "buffer" in why
    assert result["analysis_complete"] is False


def test_splitting_can_be_turned_off(tmp_path):
    """A model that can hold a whole profile should not pay for seven round trips."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.c").write_text("int main(void) { return 0; }\n")

    calls = {"n": 0}

    def count(request):
        calls["n"] += 1
        return httpx.Response(200, json=make_chat_response(content="[]"))

    s = _settings(tmp_path)
    s.scan.split_rules_by_category = False
    scan_repo(repo, s, out_dir=tmp_path / "o", llm=mock_client(count))
    assert calls["n"] == 1


def test_a_file_whose_rule_groups_mostly_succeeded_counts_as_analysed(tmp_path):
    """A live run applied six of seven rule groups to a seeded file and returned eleven
    correct findings — and reported `files_analyzed=0, files_failed=1`. Partial is its
    own state: the findings are real and the coverage is incomplete, and collapsing that
    into "failed" throws away a true result while collapsing it into "analysed" would
    hide the gap."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.c").write_text("int main(void) { return 0; }\n")

    def one_group_fails(request):
        if b"BUF-001" in request.content:
            return httpx.Response(200, json=make_chat_response(content=""))
        return httpx.Response(200, json=make_chat_response(
            content='[{"rule":"MEM-003","line":1,"severity":"critical","message":"uaf"}]'))

    s = _settings(tmp_path)
    s.scan.split_rules_by_category = True
    result = scan_repo(repo, s, out_dir=tmp_path / "o", llm=mock_client(one_group_fails))

    assert result["files_scanned"] == 1
    assert result["files_failed"] == 0, "6 of 7 groups ran — that is not a failure"
    assert result["summary"]["files_partial"] == 1
    assert result["summary"]["total"] >= 1, "the findings that were produced must survive"
    assert result["analysis_complete"] is False, "but coverage was still incomplete"

    data = json.loads(Path(result["report_json"]).read_text())
    assert "rule group(s) were not applied" in data["partial"]["a.c"]
    assert "PARTIAL COVERAGE" in Path(result["report_md"]).read_text()
