"""Tiny GitHub REST client (stdlib only) for PR comments and drift issues."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

MAX_BODY = 65000
DRIFT_LABEL = "tfscout-drift"


class GitHubError(Exception):
    pass


def _truncate(body: str) -> str:
    if len(body) <= MAX_BODY:
        return body
    return body[: MAX_BODY - 200] + "\n\n…\n\n_Report truncated (GitHub size limit). See the workflow artifact._"


@dataclass
class GitHub:
    token: str
    repo: str
    api_url: str = "https://api.github.com"

    @classmethod
    def from_env(cls, token: str = "") -> GitHub:
        token = token or os.environ.get("GITHUB_TOKEN", "")
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        if not token or not repo:
            raise GitHubError("GITHUB_TOKEN and GITHUB_REPOSITORY must be set")
        return cls(token, repo, os.environ.get("GITHUB_API_URL", "https://api.github.com"))

    def _req(self, method: str, path: str, body: Any = None, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.api_url.rstrip('/')}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "tfscout",
                **({"Content-Type": "application/json"} if data else {}),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            raise GitHubError(f"{method} {path}: HTTP {e.code} {e.read().decode(errors='replace')[:300]}") from e

    def _paginate(self, path: str, params: dict[str, Any] | None = None, limit: int = 10):
        for page in range(1, limit + 1):
            items = self._req("GET", path, params={**(params or {}), "per_page": 100, "page": page})
            if not items:
                return
            yield from items
            if len(items) < 100:
                return

    # ------------------------------------------------------------------ PR comments

    def upsert_pr_comment(self, pr: int, marker: str, body: str) -> str:
        body = _truncate(body)
        for c in self._paginate(f"/repos/{self.repo}/issues/{pr}/comments"):
            if marker in (c.get("body") or ""):
                self._req("PATCH", f"/repos/{self.repo}/issues/comments/{c['id']}", {"body": body})
                return c["html_url"]
        return self._req("POST", f"/repos/{self.repo}/issues/{pr}/comments", {"body": body})["html_url"]

    # ------------------------------------------------------------------ drift issues

    def _find_issue(self, marker: str) -> dict[str, Any] | None:
        params = {"state": "open", "labels": DRIFT_LABEL}
        for issue in self._paginate(f"/repos/{self.repo}/issues", params):
            if "pull_request" not in issue and marker in (issue.get("body") or ""):
                return issue
        return None

    def sync_drift_issue(self, marker: str, title: str, body: str, fingerprint: str) -> tuple[str, str]:
        """Create/update/close the drift issue. Returns (action, url)."""
        issue = self._find_issue(marker)
        base = f"/repos/{self.repo}/issues"
        if not fingerprint:
            if issue:
                self._req(
                    "POST",
                    f"{base}/{issue['number']}/comments",
                    {"body": "✅ Drift resolved — infrastructure matches Terraform state again."},
                )
                self._req("PATCH", f"{base}/{issue['number']}", {"state": "closed", "state_reason": "completed"})
                return "closed", issue["html_url"]
            return "none", ""
        body = _truncate(body)
        if issue:
            if f"`{fingerprint}`" in (issue.get("body") or ""):
                return "unchanged", issue["html_url"]
            self._req("PATCH", f"{base}/{issue['number']}", {"title": title, "body": body})
            self._req(
                "POST",
                f"{base}/{issue['number']}/comments",
                {"body": f"🔄 Drift changed (fingerprint `{fingerprint}`). Report updated above."},
            )
            return "updated", issue["html_url"]
        created = self._req("POST", base, {"title": title, "body": body, "labels": [DRIFT_LABEL]})
        return "created", created["html_url"]


def pr_number_from_event() -> int | None:
    path = os.environ.get("GITHUB_EVENT_PATH")
    if not path or not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        event = json.load(f)
    pr = event.get("pull_request") or {}
    num = pr.get("number") or (event.get("issue") or {}).get("number")
    return int(num) if num else None
