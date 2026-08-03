"""Best-effort must not mean invisible.

Two evaluation agents each spent a UC3 run believing the LLM triage simply had nothing to
add. In fact every call was failing — one against an endpoint unreachable from inside the
container, one against a model name that did not exist on the server — and a blanket
`except Exception: continue` made the report byte-identical to a healthy run with nothing
to report.

A third defect fed the same illusion: triage asked for `max_tokens=120`. A reasoning model
spends far more than that on `reasoning_content` before emitting a character, so the call
"succeeded" with empty content and the finding came back untriaged.

And the budget itself was misspent: `sort_key` breaks severity ties alphabetically, so on
cFS the entire `max_triage` allowance went to vendored `cFS/osal/...` headers (which sort
before `fsw/src/...`) and the application's own buffer-overflow findings were never
triaged in any run.
"""

from __future__ import annotations

import httpx

from secagent.affordances.priority import is_vendored
from secagent.agents.analysis.agent import _diagnostic, _triage, _triage_order
from secagent.agents.analysis.models import Finding

from .conftest import make_chat_response, mock_client


def _finding(file="fsw/src/fm_cmds.c", line=10, severity="high"):
    return Finding(checker="buffer-overflow", status="error", severity=severity,
                   message="possible overflow", file=file, line=line)


class _Store:
    """read_slice is the only store call triage makes."""
    repo_root = "/repo"


def _patched_slice(monkeypatch):
    from secagent.agents.analysis import agent as mod
    monkeypatch.setattr(mod.queries, "read_slice", lambda *a, **k: "int x = 0;")


# --- failures must be reported ------------------------------------------------

def test_transport_failure_is_counted_and_not_swallowed(monkeypatch, caplog):
    _patched_slice(monkeypatch)

    def boom(request):
        raise httpx.ConnectError("unreachable")

    llm = mock_client(boom)
    findings = [_finding()]
    with caplog.at_level("WARNING"):
        stats = _triage(findings, _Store(), llm, max_triage=5)
    llm.close()

    assert stats["attempted"] == 1 and stats["succeeded"] == 0 and stats["failed"] == 1
    assert findings[0].triage in (None, {}, {} if findings[0].triage == {} else None)
    assert "UNTRIAGED" in caplog.text, "silence here reads as 'nothing to say'"


def test_empty_content_counts_as_a_failure(monkeypatch, caplog):
    """HTTP 200 with no content is the reasoning-budget failure, not an assessment."""
    _patched_slice(monkeypatch)
    llm = mock_client(lambda r: httpx.Response(200, json=make_chat_response(content="")))
    with caplog.at_level("WARNING"):
        stats = _triage([_finding()], _Store(), llm, max_triage=5)
    llm.close()
    assert stats["failed"] == 1 and stats["succeeded"] == 0


def test_a_healthy_run_reports_success_and_stays_quiet(monkeypatch, caplog):
    _patched_slice(monkeypatch)
    llm = mock_client(lambda r: httpx.Response(
        200, json=make_chat_response(content="Likely a false positive: bound is checked.")))
    findings = [_finding()]
    with caplog.at_level("WARNING"):
        stats = _triage(findings, _Store(), llm, max_triage=5)
    llm.close()
    assert stats["succeeded"] == 1 and stats["failed"] == 0
    assert findings[0].triage
    assert "UNTRIAGED" not in caplog.text


def test_triage_does_not_impose_a_tiny_token_budget(monkeypatch):
    """The 120-token cap guaranteed empty content from a reasoning model."""
    _patched_slice(monkeypatch)
    seen = {}

    def handler(request):
        import json as _json
        seen["max_tokens"] = _json.loads(request.content).get("max_tokens")
        return httpx.Response(200, json=make_chat_response(content="ok"))

    llm = mock_client(handler)
    _triage([_finding()], _Store(), llm, max_triage=1)
    llm.close()
    assert seen["max_tokens"] is None or seen["max_tokens"] > 1000


# --- the budget must go to the user's own code --------------------------------

def test_vendored_paths_are_deprioritised():
    """The per-command set moved into affordances.priority — see test_priority.py."""
    assert is_vendored("cFS/osal/src/os-impl.c")
    assert is_vendored("node_modules/x/y.js")
    assert not is_vendored("fsw/src/fm_cmds.c")


def test_own_code_is_triaged_before_vendored_code_at_equal_severity():
    own = _finding(file="fsw/src/fm_cmds.c")
    vendored = _finding(file="cFS/osal/src/os-impl.c")   # sorts first alphabetically
    ordered = sorted([vendored, own], key=_triage_order)
    assert ordered[0] is own, "the budget went to somebody else's headers"


def test_severity_still_dominates():
    high_vendor = _finding(file="cFS/osal/a.c", severity="high")
    low_own = _finding(file="fsw/src/b.c", severity="low")
    assert sorted([low_own, high_vendor], key=_triage_order)[0] is high_vendor


# --- compiler diagnostics -----------------------------------------------------

def test_the_real_diagnostic_survives_not_the_trailer():
    err = (
        "src/x.c:12:10: fatal error: 'cfe.h' file not found\n"
        "1 error generated.\n"
        "ikos: error while compiling src/x.c, abort.\n"
    )
    got = _diagnostic(err)
    assert "cfe.h" in got, f"kept the useless trailer instead: {got!r}"


def test_diagnostic_falls_back_to_the_last_line():
    assert _diagnostic("something odd happened\n") == "something odd happened"
    assert _diagnostic("") == ""
