"""Tests for the Mattermost chat router (UC101) — pure dispatch, no network.

Mirrors agents/review/triggers.py's testing shape: clients are all injectable, so
routing/audit behavior is exercised without a live Mattermost, GitLab, or LLM.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from secagent.audit import AuditLogger, verify_chain
from secagent.chat.mattermost import MattermostClient
from secagent.chat.router import (
    ChatRequest,
    MattermostAuthError,
    _parse_command,
    dispatch_chat_event,
    verify_token,
)
from secagent.config import Settings

FIXTURE = Path(__file__).parent / "fixtures" / "sample_repo"


def _settings(tmp_path, **mm_overrides) -> Settings:
    s = Settings()
    s.mattermost.bot_username = "secagent"
    s.mattermost.webhook_secret = "s3cret"
    for k, v in mm_overrides.items():
        setattr(s.mattermost, k, v)
    s.audit.enabled = True
    s.audit.path = str(tmp_path / "audit.jsonl")
    return s


def _request(**overrides) -> ChatRequest:
    defaults = {
        "user_id": "u1", "user_name": "alice", "channel_id": "chan1", "text": "help",
        "team": "devteam", "post_id": "", "trigger_id": "",
    }
    defaults.update(overrides)
    return ChatRequest(**defaults)


# -- verify_token: same fail-closed contract as gitlab.webhook_secret ---------------


def test_verify_token_refuses_without_secret():
    s = Settings()  # mattermost.webhook_secret defaults to ""
    with pytest.raises(MattermostAuthError, match="webhook_secret"):
        verify_token(s, "anything")


def test_verify_token_allows_when_unauthenticated_opt_in():
    s = Settings()
    s.mattermost.webhook_allow_unauthenticated = True
    verify_token(s, "")  # should not raise


def test_verify_token_rejects_wrong_token():
    s = Settings()
    s.mattermost.webhook_secret = "s3cret"
    with pytest.raises(MattermostAuthError, match="invalid"):
        verify_token(s, "nope")


def test_verify_token_accepts_correct_token():
    s = Settings()
    s.mattermost.webhook_secret = "s3cret"
    verify_token(s, "s3cret")  # should not raise


# -- _parse_command: slash command vs. outgoing-webhook (mention-prefixed) text -----


def test_parse_command_slash_command_shape_has_no_mention_prefix():
    # Mattermost already strips "/secagent" for a slash command.
    assert _parse_command("review mygroup/myproject 42", "secagent") == (
        "review", "mygroup/myproject 42",
    )


def test_parse_command_strips_leading_mention_from_outgoing_webhook_text():
    assert _parse_command("@secagent review mygroup/myproject 42", "secagent") == (
        "review", "mygroup/myproject 42",
    )


def test_parse_command_mention_only_no_args():
    assert _parse_command("@secagent", "secagent") == ("", "")


def test_parse_command_empty_text():
    assert _parse_command("", "secagent") == ("", "")


def test_parse_command_is_case_insensitive_on_mention():
    assert _parse_command("@SecAgent help", "secagent") == ("help", "")


# -- dispatch_chat_event: routing, reply, and the audit record ----------------------


def test_dispatch_ignores_own_message(tmp_path):
    s = _settings(tmp_path)
    result = dispatch_chat_event(s, _request(user_name="secagent", text="help"))
    assert result["action"] == "ignored"
    assert result["posted"] is False
    # No audit record for a message the bot ignores as its own.
    assert not Path(s.audit.path).exists()


def test_dispatch_help_verb():
    s = Settings()
    s.audit.enabled = False
    result = dispatch_chat_event(s, _request(text="help"))
    assert result["action"] == "help"
    assert "secagent commands" in result["reply"]


def test_dispatch_unknown_verb():
    s = Settings()
    s.audit.enabled = False
    result = dispatch_chat_event(s, _request(text="frobnicate"))
    assert result["action"] == "unknown_command"
    assert "Unknown command 'frobnicate'" in result["reply"]


def test_dispatch_structure_verb_reply_and_audit_record_names_the_user(tmp_path):
    """The core UC101 contract: a mention is routed, a reply is produced, and the
    resulting audit record is attributable to the actual Mattermost user — not the
    bot's own service principal."""
    s = _settings(tmp_path)
    s.affordances.llm_summaries = False
    s.affordances.store_dir = str(tmp_path / "store")

    result = dispatch_chat_event(
        s, _request(
            user_id="u42", user_name="alice", channel_id="town-square",
            text=f"@secagent structure {FIXTURE}", post_id="post-abc",
        ),
    )

    assert result["action"] == "structure"
    assert result["reply"]  # a real structure outline came back

    ok, msg = verify_chain(s.audit.path)
    assert ok, msg
    records = [json.loads(x) for x in Path(s.audit.path).read_text().splitlines()]
    structure_records = [r for r in records if r["action"] == "structure"]
    assert len(structure_records) == 1
    rec = structure_records[0]
    assert rec["end_user"] == "mattermost:alice"  # NOT the service principal
    assert rec["target"]["channel"] == "town-square"
    assert rec["target"]["thread"] == "post-abc"
    assert rec["outcome"] == "ok"
    assert rec["cui"] is False  # capture_content defaults to False
    # Default metadata mode: a hash, never the verbatim message.
    assert rec["target"]["message_sha256"]
    assert "message" not in rec["target"]


def test_dispatch_structure_verb_captures_content_when_configured(tmp_path):
    s = _settings(tmp_path)
    s.affordances.llm_summaries = False
    s.affordances.store_dir = str(tmp_path / "store")
    s.audit.capture_content = True

    dispatch_chat_event(
        s, _request(user_name="bob", text=f"structure {FIXTURE}"),
    )
    records = [json.loads(x) for x in Path(s.audit.path).read_text().splitlines()]
    rec = next(r for r in records if r["action"] == "structure")
    assert rec["cui"] is True
    assert rec["target"]["message"] == f"structure {FIXTURE}"


def test_dispatch_structure_missing_repo_arg_is_a_usage_error(tmp_path):
    s = _settings(tmp_path)
    result = dispatch_chat_event(s, _request(text="structure"))
    assert result["action"] == "structure_usage_error"
    assert "Usage" in result["reply"]
    rec = json.loads(Path(s.audit.path).read_text().splitlines()[0])
    assert rec["outcome"] == "error"


def test_dispatch_review_usage_error_wrong_arg_count(tmp_path):
    s = _settings(tmp_path)
    result = dispatch_chat_event(s, _request(text="review only-one-arg"))
    assert result["action"] == "review_usage_error"
    assert "Usage" in result["reply"]


def test_dispatch_review_usage_error_non_numeric_iid(tmp_path):
    s = _settings(tmp_path)
    result = dispatch_chat_event(s, _request(text="review group/project not-a-number"))
    assert result["action"] == "review_usage_error"


def test_dispatch_review_verb_error_path_does_not_crash_and_is_audited(tmp_path):
    """No live GitLab/LLM configured: review_merge_request must fail, and that
    failure must become a friendly reply + an 'error' outcome audit record, not an
    unhandled exception from the chat handler."""
    s = _settings(tmp_path)
    result = dispatch_chat_event(
        s, _request(text="review mygroup/myproject 42"), gitlab=object(), llm=object(),
    )
    assert result["action"] == "review_error"
    assert "Could not review" in result["reply"]
    rec = json.loads(Path(s.audit.path).read_text().splitlines()[0])
    assert rec["outcome"] == "error"
    assert rec["end_user"] == "mattermost:alice"


def test_dispatch_posts_reply_via_injected_mattermost_client(tmp_path):
    import httpx

    posted = {}

    def handler(request: httpx.Request) -> httpx.Response:
        posted["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "post123"})

    s = _settings(tmp_path)
    mm = MattermostClient(
        s.mattermost,
        http=httpx.Client(base_url="http://mock/api/v4/", transport=httpx.MockTransport(handler)),
    )
    result = dispatch_chat_event(
        s, _request(text="help", channel_id="chan42", post_id="root99"), mattermost=mm,
    )
    assert result["posted"] is True
    assert posted["body"]["channel_id"] == "chan42"
    assert posted["body"]["root_id"] == "root99"
    assert "secagent commands" in posted["body"]["message"]


def test_dispatch_records_post_failure_as_error_outcome_but_still_returns_reply(tmp_path):
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="mattermost is down")

    s = _settings(tmp_path)
    mm = MattermostClient(
        s.mattermost,
        http=httpx.Client(base_url="http://mock/api/v4/", transport=httpx.MockTransport(handler)),
    )
    result = dispatch_chat_event(s, _request(text="help"), mattermost=mm)

    assert result["posted"] is False
    assert result["post_error"]
    assert result["reply"]  # the reply text is still returned even though posting failed
    rec = json.loads(Path(s.audit.path).read_text().splitlines()[0])
    assert rec["outcome"] == "error"


def test_dispatch_chain_still_verifies_across_multiple_events(tmp_path):
    s = _settings(tmp_path)
    dispatch_chat_event(s, _request(text="help", user_name="alice"))
    dispatch_chat_event(s, _request(text="frobnicate", user_name="bob"))
    ok, msg = verify_chain(s.audit.path)
    assert ok, msg


def test_dispatch_uses_injected_audit_logger(tmp_path):
    log_path = tmp_path / "custom-audit.jsonl"
    logger = AuditLogger(log_path, enabled=True, principal="service:secchat-bot")
    s = Settings()
    dispatch_chat_event(s, _request(text="help"), audit=logger)
    ok, _ = verify_chain(log_path)
    assert ok
    rec = json.loads(log_path.read_text().splitlines()[0])
    assert rec["principal"] == "service:secchat-bot"
    assert rec["end_user"] == "mattermost:alice"
