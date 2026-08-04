"""Route one normalized Mattermost inbound event to a secagent use case, reply
in-thread, and audit the interaction (UC101).

Pure dispatch: the Mattermost/GitLab/LLM/audit clients are all injectable, so this is
unit-testable without network access — mirrors ``agents/review/triggers.py``'s split
from ``agents/review/webhook.py``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ..audit import AuditLogger, get_audit_logger
from ..config import Settings
from .mattermost import MattermostClient

log = logging.getLogger(__name__)

# Headroom under Mattermost's default 16383-character post limit. Never silently
# truncate without saying so — matches this codebase's established stance on
# disclosed truncation (see docs/use-cases.md's testgen/scan sections).
_MAX_REPLY_CHARS = 12_000
_TRUNCATION_NOTE = "\n\n*(reply truncated — see the full output via the CLI)*"


class MattermostAuthError(RuntimeError):
    """No usable webhook/command token configured, or the supplied one is wrong."""


@dataclass
class ChatRequest:
    """A normalized Mattermost inbound event — the same shape whether it arrived as a
    slash command or an outgoing (trigger-word) webhook; see ``webhook.py`` for how
    each is mapped onto this."""

    user_id: str
    user_name: str
    channel_id: str
    text: str
    team: str = ""
    post_id: str = ""       # outgoing webhook: the triggering post: threads the reply
    trigger_id: str = ""    # slash command only


def verify_token(settings: Settings, supplied: str) -> None:
    """Constant-time shared-secret check, mirroring GitLab's webhook auth exactly
    (``agents/review/webhook.py``): a missing secret refuses unless explicitly opted
    into an open endpoint, and the compare never short-circuits on a wrong prefix."""
    import hmac

    secret = settings.mattermost.webhook_secret
    if not secret:
        if settings.mattermost.webhook_allow_unauthenticated:
            return
        raise MattermostAuthError(
            "no mattermost.webhook_secret configured — refusing to treat this request "
            "as authenticated. Set SECAGENT_MATTERMOST__WEBHOOK_SECRET to the token "
            "configured on the Mattermost slash command / outgoing webhook, or set "
            "mattermost.webhook_allow_unauthenticated=true if you really do mean an "
            "open endpoint (e.g. behind an authenticating proxy)."
        )
    if not hmac.compare_digest(supplied or "", secret):
        raise MattermostAuthError("invalid webhook/command token")


def _parse_command(text: str, bot_username: str) -> tuple[str, str]:
    """Split inbound text into ``(verb, rest)``.

    An outgoing (trigger-word) webhook's ``text`` includes the mention itself (e.g.
    ``"@secagent review a/b 42"``); a slash command's ``text`` does not (Mattermost
    already stripped ``/secagent``). Tolerating an optional leading mention here lets
    one parser serve both delivery mechanisms.
    """
    tokens = (text or "").strip().split(maxsplit=1)
    if not tokens:
        return "", ""
    first = tokens[0].lstrip("@").lower()
    if first == bot_username.lower():
        rest = tokens[1] if len(tokens) > 1 else ""
        tokens = rest.strip().split(maxsplit=1)
        if not tokens:
            return "", ""
    verb = tokens[0].lower()
    rest = tokens[1] if len(tokens) > 1 else ""
    return verb, rest


def _help_text() -> str:
    return (
        "**secagent commands:**\n"
        "- `review <project> <mr_iid>` — generate a merge-request review (UC100), "
        "e.g. `review mygroup/myproject 42`\n"
        "- `structure <repo-path>` — project structure outline (affordances)\n"
        "- `help` — this message"
    )


def _handle_review(
    settings: Settings, rest: str, *, gitlab: Any = None, llm: Any = None,
) -> tuple[str, str]:
    parts = rest.split()
    if len(parts) != 2:
        return "Usage: `review <project> <mr_iid>`", "review_usage_error"
    project, mr_iid_s = parts
    try:
        mr_iid = int(mr_iid_s)
    except ValueError:
        return f"'{mr_iid_s}' is not a valid merge request number.", "review_usage_error"
    from ..agents.review.agent import review_merge_request

    try:
        # post=False: the reply IS the delivery — a chat-triggered review is not also
        # posted to GitLab, so one Mattermost invocation does not silently write to a
        # second system.
        result = review_merge_request(
            settings, project=project, mr_iid=mr_iid, post=False, gitlab=gitlab, llm=llm,
        )
    except Exception as exc:  # noqa: BLE001 - one bad request must not crash the bot
        log.error("chat review of %s!%s failed: %s", project, mr_iid, exc)
        return f"Could not review {project}!{mr_iid}: {exc}", "review_error"
    return result["review"], "review_mr"


def _handle_structure(settings: Settings, rest: str) -> tuple[str, str]:
    repo = rest.strip()
    if not repo:
        return "Usage: `structure <repo-path>`", "structure_usage_error"
    from ..affordances import queries

    try:
        store = queries.ensure_indexed(repo, settings)
    except Exception as exc:  # noqa: BLE001 - one bad request must not crash the bot
        log.error("chat structure query of %s failed: %s", repo, exc)
        return f"Could not index {repo}: {exc}", "structure_error"
    try:
        return queries.structure(store), "structure"
    finally:
        store.close()


def _truncate(text: str) -> str:
    if len(text) <= _MAX_REPLY_CHARS:
        return text
    return text[: _MAX_REPLY_CHARS - len(_TRUNCATION_NOTE)] + _TRUNCATION_NOTE


def dispatch_chat_event(
    settings: Settings,
    request: ChatRequest,
    *,
    mattermost: MattermostClient | None = None,
    gitlab: Any = None,
    llm: Any = None,
    audit: AuditLogger | None = None,
) -> dict[str, Any]:
    """Route one normalized chat request to a use case, reply, and audit it.

    ``mattermost`` posts the reply when supplied; when ``None`` the reply is computed
    and audited but not delivered anywhere (used by tests that only care about
    routing/audit, and by any future caller that wants the text without the side
    effect). ``gitlab``/``llm`` are threaded straight through to the ``review`` verb
    for offline testing, exactly as ``agents/review/triggers.dispatch_event`` does.

    A freshly-built (``audit=None``) logger is constructed LATE — right before the
    one ``record_chat`` call below, not at the top of this function. The verb
    handlers can themselves trigger other audited actions first (``structure`` ->
    ``ensure_indexed`` -> ``index_repo``'s own "index" record; ``review`` ->
    ``review_merge_request``'s own "review_mr" record), each via its own fresh
    ``get_audit_logger(settings)`` call. `AuditLogger._prev_hash` is captured once,
    at construction — building ours early and holding onto it across those nested
    writes would chain it onto a hash that is no longer the file's actual tail,
    breaking `verify_chain`. Building it last (matching where
    ``agents/review/agent.py`` places its own `get_audit_logger(settings).record(...)`
    call — after all the real work, not before) reads the true tail.
    """
    bot = settings.mattermost.bot_username
    if request.user_name and request.user_name.lower() == bot.lower():
        return {"action": "ignored", "reason": "own message", "posted": False}

    verb, rest = _parse_command(request.text, bot)
    if verb in ("", "help"):
        reply, action = _help_text(), "help"
    elif verb == "review":
        reply, action = _handle_review(settings, rest, gitlab=gitlab, llm=llm)
    elif verb == "structure":
        reply, action = _handle_structure(settings, rest)
    else:
        reply, action = f"Unknown command '{verb}'. " + _help_text(), "unknown_command"

    reply = _truncate(reply)

    posted = False
    post_error: str | None = None
    if mattermost is not None:
        try:
            mattermost.create_post(request.channel_id, reply, root_id=request.post_id)
            posted = True
        except Exception as exc:  # noqa: BLE001 - the audit record must still be written
            post_error = str(exc)
            log.error(
                "chat: failed to post reply to channel %s: %s", request.channel_id, exc
            )

    end_user = f"mattermost:{request.user_name or request.user_id}"
    outcome = "error" if post_error or action.endswith("_error") else "ok"
    audit = audit or get_audit_logger(settings)  # see the docstring: built late, on purpose
    audit.record_chat(
        action,
        end_user=end_user,
        channel=request.channel_id,
        thread=request.post_id or request.trigger_id,
        message=request.text,
        reply=reply,
        outcome=outcome,
        detail=post_error or "",
    )
    return {"action": action, "reply": reply, "posted": posted, "post_error": post_error}
