"""GitHub skill tools (GitHub REST API)."""
from __future__ import annotations

from typing import Any

from agentkai.skills_bundle._common import (
    auth_error, get_token, test_transport,
)
from agentkai.skills_bundle._http import HttpClient, HttpError
from agentkai.tools import Tool

ENV_VAR = "GITHUB_TOKEN"
TOKEN_FILE = "github_token.json"
BASE = "https://api.github.com"
API_HEADERS = {"Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}


def _client(config: dict | None) -> HttpClient | None:
    token = get_token(config, ENV_VAR, TOKEN_FILE)
    if not token:
        return None
    return HttpClient(BASE,
                      headers={**API_HEADERS,
                               "Authorization": f"Bearer {token}"},
                      transport=test_transport(config))


def _no_auth() -> str:
    return auth_error("github", ENV_VAR, TOKEN_FILE)


def _issue(i: dict) -> dict:
    return {
        "number": i.get("number"),
        "title": i.get("title"),
        "state": i.get("state"),
        "is_pull_request": "pull_request" in i,
        "user": ((i.get("user") or {}).get("login")),
        "labels": [l.get("name") for l in i.get("labels", []) or []],
        "url": i.get("html_url"),
    }


def get_tools(config: dict | None = None) -> list[Tool]:
    def github_list_repos() -> list | str:
        """List the authenticated user's repos (name, visibility, updated)."""
        client = _client(config)
        if client is None:
            return _no_auth()
        try:
            data = client.request("GET", "/user/repos",
                                  params={"per_page": "100",
                                          "sort": "updated"})
        except HttpError as exc:
            return f"ERROR: github list repos failed: {exc}"
        return [{"full_name": r.get("full_name"),
                 "description": (r.get("description") or "")[:200],
                 "private": r.get("private"),
                 "updated_at": r.get("updated_at")}
                for r in (data or [])]

    def github_list_issues(owner: str, repo: str,
                           state: str = "open") -> list | str:
        """List issues (and PRs) on a repo. state: open|closed|all."""
        client = _client(config)
        if client is None:
            return _no_auth()
        if state not in ("open", "closed", "all"):
            return "ERROR: state must be open, closed or all"
        try:
            data = client.request(
                "GET", f"/repos/{owner}/{repo}/issues",
                params={"state": state, "per_page": "50"})
        except HttpError as exc:
            return f"ERROR: github list issues failed: {exc}"
        return [_issue(i) for i in (data or [])]

    def github_create_issue(owner: str, repo: str, title: str,
                            body: str = "") -> dict | str:
        """Create an issue. HIGH RISK: public action, always gated."""
        client = _client(config)
        if client is None:
            return _no_auth()
        if not title.strip():
            return "ERROR: title is required"
        try:
            data = client.request(
                "POST", f"/repos/{owner}/{repo}/issues",
                json_body={"title": title, "body": body})
        except HttpError as exc:
            return f"ERROR: github create issue failed: {exc}"
        return {"number": (data or {}).get("number"),
                "url": (data or {}).get("html_url")}

    def github_list_prs(owner: str, repo: str,
                        state: str = "open") -> list | str:
        """List pull requests on a repo. state: open|closed|all."""
        client = _client(config)
        if client is None:
            return _no_auth()
        if state not in ("open", "closed", "all"):
            return "ERROR: state must be open, closed or all"
        try:
            data = client.request(
                "GET", f"/repos/{owner}/{repo}/pulls",
                params={"state": state, "per_page": "50"})
        except HttpError as exc:
            return f"ERROR: github list prs failed: {exc}"
        return [{"number": p.get("number"),
                 "title": p.get("title"),
                 "state": p.get("state"),
                 "user": ((p.get("user") or {}).get("login")),
                 "head": ((p.get("head") or {}).get("ref")),
                 "base": ((p.get("base") or {}).get("ref")),
                 "merged": p.get("merged_at") is not None,
                 "url": p.get("html_url")}
                for p in (data or [])]

    def github_open_pr(owner: str, repo: str, title: str, head: str,
                       base: str, body: str = "") -> dict | str:
        """Open a pull request head→base. HIGH RISK: public action."""
        client = _client(config)
        if client is None:
            return _no_auth()
        if not all([title.strip(), head.strip(), base.strip()]):
            return "ERROR: title, head and base are required"
        try:
            data = client.request(
                "POST", f"/repos/{owner}/{repo}/pulls",
                json_body={"title": title, "head": head,
                           "base": base, "body": body})
        except HttpError as exc:
            return f"ERROR: github open pr failed: {exc}"
        return {"number": (data or {}).get("number"),
                "url": (data or {}).get("html_url")}

    def github_comment_issue(owner: str, repo: str, number: int,
                             body: str = "") -> dict | str:
        """Comment on an issue or PR. HIGH RISK: public action."""
        client = _client(config)
        if client is None:
            return _no_auth()
        if not body.strip():
            return "ERROR: body is required"
        try:
            data = client.request(
                "POST", f"/repos/{owner}/{repo}/issues/{number}/comments",
                json_body={"body": body})
        except HttpError as exc:
            return f"ERROR: github comment failed: {exc}"
        return {"comment_id": (data or {}).get("id"),
                "url": (data or {}).get("html_url")}

    def github_list_workflow_runs(owner: str, repo: str,
                                  per_page: int = 20) -> list | str:
        """List recent Actions workflow runs (status, conclusion, branch)."""
        client = _client(config)
        if client is None:
            return _no_auth()
        try:
            data = client.request(
                "GET", f"/repos/{owner}/{repo}/actions/runs",
                params={"per_page": str(min(max(int(per_page), 1), 50))})
        except HttpError as exc:
            return f"ERROR: github list workflow runs failed: {exc}"
        runs = (data or {}).get("workflow_runs", []) or []
        return [{"id": r.get("id"),
                 "name": r.get("name"),
                 "status": r.get("status"),
                 "conclusion": r.get("conclusion"),
                 "head_branch": r.get("head_branch"),
                 "event": r.get("event"),
                 "created_at": r.get("created_at"),
                 "url": r.get("html_url")}
                for r in runs]

    def _t(name: str, desc: str, props: dict, required: list[str],
           risk: str, func: Any) -> Tool:
        return Tool(name=name, description=desc,
                    json_schema={"type": "object", "properties": props,
                                 "required": required},
                    risk=risk, func=func)  # type: ignore[arg-type]

    repo_props = lambda extra=None: {  # noqa: E731
        "owner": {"type": "string"}, "repo": {"type": "string"},
        **(extra or {})}

    return [
        Tool(
            name="github_list_repos",
            description="List the authenticated user's GitHub repositories.",
            json_schema={"type": "object", "properties": {}},
            risk="low", func=github_list_repos),
        _t("github_list_issues",
           "List issues (and PRs) on a GitHub repo.",
           repo_props({"state": {"type": "string",
                                 "enum": ["open", "closed", "all"]}}),
           ["owner", "repo"], "low", github_list_issues),
        _t("github_create_issue",
           "Create a GitHub issue. HIGH RISK: public action, gated.",
           repo_props({"title": {"type": "string"},
                       "body": {"type": "string"}}),
           ["owner", "repo", "title"], "high", github_create_issue),
        _t("github_list_prs",
           "List pull requests on a GitHub repo.",
           repo_props({"state": {"type": "string",
                                 "enum": ["open", "closed", "all"]}}),
           ["owner", "repo"], "low", github_list_prs),
        _t("github_open_pr",
           "Open a pull request from head branch into base. HIGH RISK: "
           "public action, gated. head must already exist on the remote.",
           repo_props({"title": {"type": "string"},
                       "head": {"type": "string"},
                       "base": {"type": "string"},
                       "body": {"type": "string"}}),
           ["owner", "repo", "title", "head", "base"], "high", github_open_pr),
        _t("github_comment_issue",
           "Comment on a GitHub issue or PR. HIGH RISK: public action, gated.",
           repo_props({"number": {"type": "integer"},
                       "body": {"type": "string"}}),
           ["owner", "repo", "number", "body"], "high", github_comment_issue),
        _t("github_list_workflow_runs",
           "List recent GitHub Actions workflow runs on a repo.",
           repo_props({"per_page": {"type": "integer"}}),
           ["owner", "repo"], "low", github_list_workflow_runs),
    ]


__all__ = ["get_tools", "ENV_VAR", "TOKEN_FILE"]
