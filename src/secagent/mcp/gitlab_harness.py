"""GitLab REST v4 harness.

A small, FIPS-friendly (system-OpenSSL TLS, no extra crypto) wrapper over the GitLab
API for merge-request review, plus a ToolRegistry so the same surface can be exposed
over MCP. This is the "GitLab API MCP harness" UC100 is built on; it can also be
pointed at GitLab's official MCP server instead.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

import httpx

from ..config import GitLabConfig
from ..toolkit import ToolRegistry


class GitLabError(RuntimeError):
    pass


class GitLabClient:
    def __init__(self, config: GitLabConfig, http: httpx.Client | None = None) -> None:
        self.config = config
        self.api = config.url.rstrip("/") + "/api/v4"
        self._owns_http = http is None
        # Trailing slash on base_url + relative request paths so httpx preserves the
        # "/api/v4" prefix (a leading-slash path would otherwise replace it).
        self._http = http or httpx.Client(
            base_url=self.api + "/",
            headers={"PRIVATE-TOKEN": config.token},
            timeout=30.0,
            verify=config.verify_tls,  # system OpenSSL / FIPS trust store
        )

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> GitLabClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @staticmethod
    def _pid(project: str | int) -> str:
        # Numeric id passes through; "group/name" path is URL-encoded.
        return quote(str(project), safe="")

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        resp = self._http.get(path.lstrip("/"), params=params)
        if resp.status_code >= 400:
            raise GitLabError(f"GET {path} -> {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    def _post(self, path: str, data: dict[str, Any]) -> Any:
        resp = self._http.post(path.lstrip("/"), data=data)
        if resp.status_code >= 400:
            raise GitLabError(f"POST {path} -> {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    # -- MR operations -------------------------------------------------------
    def get_merge_request(self, project: str | int, iid: int) -> dict[str, Any]:
        return self._get(f"/projects/{self._pid(project)}/merge_requests/{iid}")

    def get_merge_request_changes(self, project: str | int, iid: int) -> dict[str, Any]:
        return self._get(f"/projects/{self._pid(project)}/merge_requests/{iid}/changes")

    def list_discussions(self, project: str | int, iid: int) -> list[dict[str, Any]]:
        return self._get(
            f"/projects/{self._pid(project)}/merge_requests/{iid}/discussions",
            params={"per_page": 100},
        )

    def list_open_merge_requests(self, project: str | int) -> list[dict[str, Any]]:
        return self._get(
            f"/projects/{self._pid(project)}/merge_requests",
            params={"state": "opened", "per_page": 50},
        )

    def create_note(self, project: str | int, iid: int, body: str) -> dict[str, Any]:
        return self._post(
            f"/projects/{self._pid(project)}/merge_requests/{iid}/notes", {"body": body}
        )

    def reply_to_discussion(
        self, project: str | int, iid: int, discussion_id: str, body: str
    ) -> dict[str, Any]:
        return self._post(
            f"/projects/{self._pid(project)}/merge_requests/{iid}/discussions/"
            f"{discussion_id}/notes",
            {"body": body},
        )

    def changed_paths(self, changes: dict[str, Any]) -> list[str]:
        return [c.get("new_path") or c.get("old_path") for c in changes.get("changes", [])]


def build_gitlab_tools(client: GitLabClient) -> ToolRegistry:
    """Expose the GitLab harness as MCP tools."""
    reg = ToolRegistry()
    proj = {"type": "string", "description": "project id or group/name path"}
    iid = {"type": "integer", "description": "merge request iid"}

    reg.add("gitlab_get_mr", "Get a merge request's metadata.",
            {"type": "object", "properties": {"project": proj, "iid": iid},
             "required": ["project", "iid"]},
            lambda a: json.dumps(client.get_merge_request(a["project"], int(a["iid"]))))
    reg.add("gitlab_get_mr_changes", "Get a merge request's file changes/diffs.",
            {"type": "object", "properties": {"project": proj, "iid": iid},
             "required": ["project", "iid"]},
            lambda a: json.dumps(client.get_merge_request_changes(a["project"], int(a["iid"]))))
    reg.add("gitlab_list_discussions", "List discussion threads on a merge request.",
            {"type": "object", "properties": {"project": proj, "iid": iid},
             "required": ["project", "iid"]},
            lambda a: json.dumps(client.list_discussions(a["project"], int(a["iid"]))))
    reg.add("gitlab_create_note", "Post a top-level note on a merge request.",
            {"type": "object", "properties": {"project": proj, "iid": iid,
             "body": {"type": "string"}}, "required": ["project", "iid", "body"]},
            lambda a: json.dumps(client.create_note(a["project"], int(a["iid"]), a["body"])))
    reg.add("gitlab_reply_discussion", "Reply within a merge request discussion thread.",
            {"type": "object", "properties": {"project": proj, "iid": iid,
             "discussion_id": {"type": "string"}, "body": {"type": "string"}},
             "required": ["project", "iid", "discussion_id", "body"]},
            lambda a: json.dumps(
                client.reply_to_discussion(
                    a["project"], int(a["iid"]), a["discussion_id"], a["body"])))
    return reg
