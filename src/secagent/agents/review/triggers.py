"""Map GitLab webhook events to review actions.

Two triggers:
* A new/reopened merge request -> post one initial review.
* A note that @-mentions the bot -> reply in that discussion thread.

These are pure functions (GitLab/LLM clients are injectable) so they unit-test
without network access.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ...config import Settings
from ...mcp.gitlab_harness import GitLabClient
from .agent import respond_to_mention, review_merge_request

_REVIEW_ACTIONS = {"open", "reopen"}


def is_mention(note_body: str, bot_username: str) -> bool:
    return f"@{bot_username}".lower() in (note_body or "").lower()


def dispatch_event(
    settings: Settings,
    event: str,
    payload: dict[str, Any],
    *,
    repo: str | Path | None = None,
    post: bool = True,
    gitlab: GitLabClient | None = None,
    llm: GitLabClient | None = None,
) -> dict[str, Any]:
    """Route a webhook event to the right handler. Returns an action report."""
    kind = payload.get("object_kind") or event
    if kind == "merge_request":
        return handle_merge_request_event(
            settings, payload, repo=repo, post=post, gitlab=gitlab, llm=llm
        )
    if kind == "note":
        return handle_note_event(
            settings, payload, repo=repo, post=post, gitlab=gitlab, llm=llm
        )
    return {"action": "ignored", "reason": f"unhandled event kind: {kind}"}


def handle_merge_request_event(
    settings: Settings, payload: dict[str, Any], *, repo=None, post=True, gitlab=None, llm=None
) -> dict[str, Any]:
    attrs = payload.get("object_attributes", {})
    action = attrs.get("action")
    if action not in _REVIEW_ACTIONS:
        return {"action": "ignored", "reason": f"MR action '{action}' not reviewed"}
    project = _project_id(payload)
    iid = attrs.get("iid")
    if project is None or iid is None:
        return {"action": "ignored", "reason": "missing project/iid"}
    result = review_merge_request(
        settings, project=project, mr_iid=int(iid), repo=repo, post=post,
        gitlab=gitlab, llm=llm,
    )
    return {"action": "reviewed", **result}


def handle_note_event(
    settings: Settings, payload: dict[str, Any], *, repo=None, post=True, gitlab=None, llm=None
) -> dict[str, Any]:
    attrs = payload.get("object_attributes", {})
    if attrs.get("noteable_type") != "MergeRequest":
        return {"action": "ignored", "reason": "note not on a merge request"}
    body = attrs.get("note", "")
    author = (payload.get("user") or {}).get("username", "")
    bot = settings.gitlab.bot_username
    # Don't answer our own notes; only respond when explicitly mentioned.
    if author == bot:
        return {"action": "ignored", "reason": "own note"}
    if not is_mention(body, bot):
        return {"action": "ignored", "reason": "not mentioned"}
    project = _project_id(payload)
    iid = (payload.get("merge_request") or {}).get("iid")
    discussion_id = attrs.get("discussion_id")
    if project is None or iid is None or not discussion_id:
        return {"action": "ignored", "reason": "missing project/iid/discussion"}
    result = respond_to_mention(
        settings, project=project, mr_iid=int(iid), discussion_id=discussion_id,
        thread=body, repo=repo, post=post, gitlab=gitlab, llm=llm,
    )
    return {"action": "replied", **result}


def _project_id(payload: dict[str, Any]) -> str | None:
    project = payload.get("project") or {}
    pid = project.get("id")
    if pid is not None:
        return str(pid)
    return project.get("path_with_namespace")
