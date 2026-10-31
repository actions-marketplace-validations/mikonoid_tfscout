import json
from pathlib import Path

import pytest

from tfscout import cli
from tfscout.github import DRIFT_LABEL, GitHub, pr_number_from_event

FIXTURES = Path(__file__).parent / "fixtures"

REVIEW = str(FIXTURES / "plan_review.json")
DRIFT = str(FIXTURES / "plan_drift.json")


class FakeGitHub(GitHub):
    def __init__(self, existing=None):
        super().__init__("t", "o/r")
        self.existing = existing or []
        self.calls = []

    def _req(self, method, path, body=None, params=None):
        self.calls.append((method, path, body))
        if method == "GET":
            return self.existing if (params or {}).get("page") == 1 else []
        return {"html_url": "https://gh/x", "id": 1}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    for k in ("GITHUB_OUTPUT", "GITHUB_STEP_SUMMARY", "GITHUB_EVENT_PATH", "TFSCOUT_PROVIDER"):
        monkeypatch.delenv(k, raising=False)


def test_review_cli_markdown(capsys):
    assert cli.main(["review", REVIEW]) == 0
    assert "tfscout plan review" in capsys.readouterr().out


def test_review_cli_json_and_fail_on(capsys):
    assert cli.main(["review", REVIEW, "--format", "json", "--fail-on", "high"]) == cli.EXIT_FAIL_ON
    data = json.loads(capsys.readouterr().out)
    assert data["max_severity"] == "critical"


def test_drift_cli_exit_code_and_outputs(tmp_path, monkeypatch, capsys):
    out, summary = tmp_path / "out", tmp_path / "summary"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert cli.main(["drift", DRIFT, "--exit-code", "-o", str(tmp_path / "r.md")]) == cli.EXIT_DRIFT
    assert "drift-detected=true" in out.read_text()
    assert "drift report" in summary.read_text()
    assert (tmp_path / "r.md").exists()


def test_bad_plan(capsys):
    assert cli.main(["review", "/nonexistent.json"]) == 1


def test_upsert_comment_updates_existing():
    gh = FakeGitHub(existing=[{"id": 7, "body": "x <!-- tfscout:review:p -->", "html_url": "u"}])
    assert gh.upsert_pr_comment(5, "<!-- tfscout:review:p -->", "new") == "u"
    assert ("PATCH", "/repos/o/r/issues/comments/7", {"body": "new"}) in gh.calls


def test_upsert_comment_creates():
    gh = FakeGitHub()
    gh.upsert_pr_comment(5, "<!-- m -->", "new")
    assert gh.calls[-1][:2] == ("POST", "/repos/o/r/issues/5/comments")


def test_drift_issue_lifecycle():
    gh = FakeGitHub()
    assert gh.sync_drift_issue("<!-- m -->", "t", "body `abc`", "abc")[0] == "created"
    assert gh.calls[-1][2]["labels"] == [DRIFT_LABEL]

    from tfscout import __version__

    issue = {"number": 3, "body": f"<!-- m --> fingerprint: `abc` tfscout v{__version__}, rules", "html_url": "u"}
    assert FakeGitHub([issue]).sync_drift_issue("<!-- m -->", "t", "b", "abc")[0] == "unchanged"
    old = {"number": 3, "body": "<!-- m --> fingerprint: `abc` tfscout v0.0.1, rules", "html_url": "u"}
    gh = FakeGitHub([old])
    assert gh.sync_drift_issue("<!-- m -->", "t", "b", "abc")[0] == "refreshed"
    assert not any(c[1].endswith("/comments") and c[0] == "POST" for c in gh.calls)
    assert FakeGitHub([issue]).sync_drift_issue("<!-- m -->", "t", "b", "def")[0] == "updated"
    gh = FakeGitHub([issue])
    assert gh.sync_drift_issue("<!-- m -->", "t", "b", "")[0] == "closed"
    assert ("PATCH", "/repos/o/r/issues/3", {"state": "closed", "state_reason": "completed"}) in gh.calls
    assert FakeGitHub().sync_drift_issue("<!-- m -->", "t", "b", "")[0] == "none"


def test_drift_issue_ignores_pull_requests():
    pr = {"number": 4, "body": "<!-- m -->", "pull_request": {}, "html_url": "u"}
    assert FakeGitHub([pr]).sync_drift_issue("<!-- m -->", "t", "b", "x")[0] == "created"


def test_pr_number_from_event(tmp_path, monkeypatch):
    ev = tmp_path / "event.json"
    ev.write_text(json.dumps({"pull_request": {"number": 42}}))
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(ev))
    assert pr_number_from_event() == 42


def test_missing_api_key_falls_back_to_rules(monkeypatch, capsys):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("TFSCOUT_API_KEY", raising=False)
    assert cli.main(["review", REVIEW, "--provider", "gemini"]) == 0
    captured = capsys.readouterr()
    assert "AI disabled" in captured.err and "rules only" in captured.out
