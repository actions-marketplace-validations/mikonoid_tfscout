"""Command line interface: `tfscout review` and `tfscout drift`."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from tfscout import __version__
from tfscout.analyze import run_drift, run_review
from tfscout.github import GitHub, GitHubError, pr_number_from_event
from tfscout.llm import PROVIDERS, LLMError, make_client
from tfscout.plan import PlanError, load_plan
from tfscout.render import (
    drift_json,
    drift_marker,
    render_drift_md,
    render_review_md,
    review_json,
    review_marker,
)
from tfscout.rules import SEVERITIES, sev_rank

EXIT_FAIL_ON = 3
EXIT_DRIFT = 2


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("plan", help="plan file: JSON from `terraform show -json`, or a binary plan (-out=...)")
    p.add_argument("--name", default="", help="label for this root module (e.g. 'prod/us-east-1')")
    p.add_argument(
        "--provider",
        default=os.environ.get("TFSCOUT_PROVIDER", "none"),
        choices=PROVIDERS,
        help="LLM provider; 'none' = deterministic rules only (default: $TFSCOUT_PROVIDER or none)",
    )
    p.add_argument("--model", default=os.environ.get("TFSCOUT_MODEL", ""), help="override the model name")
    p.add_argument(
        "--base-url", default=os.environ.get("TFSCOUT_BASE_URL", ""), help="override the OpenAI-compatible endpoint"
    )
    p.add_argument("--api-key", default="", help="API key (prefer env TFSCOUT_API_KEY or provider env var)")
    p.add_argument(
        "--timeout",
        type=float,
        default=float(os.environ.get("TFSCOUT_TIMEOUT", "90")),
        help="seconds to wait for the LLM per attempt (default: 90)",
    )
    p.add_argument(
        "--reasoning-effort",
        default=os.environ.get("TFSCOUT_REASONING_EFFORT", ""),
        help="reasoning_effort for thinking models: minimal|low|medium|high (gemini default: low)",
    )
    p.add_argument("--format", choices=["markdown", "json"], default="markdown")
    p.add_argument("--output", "-o", default="", help="write the report to a file instead of stdout")
    p.add_argument("--terraform-bin", default="terraform", help="terraform/tofu binary for binary plans")
    p.add_argument("--chdir", default=None, help="working dir for `terraform show` on binary plans")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tfscout", description="AI-assisted Terraform plan review and drift detection."
    )
    parser.add_argument("--version", action="version", version=f"tfscout {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    r = sub.add_parser("review", help="review a plan: risks, security, data loss")
    _common(r)
    r.add_argument("--github-comment", action="store_true", help="create/update a PR comment")
    r.add_argument("--pr", type=int, default=None, help="PR number (default: from GITHUB_EVENT_PATH)")
    r.add_argument(
        "--fail-on",
        choices=["none", *SEVERITIES],
        default="none",
        help=f"exit {EXIT_FAIL_ON} if a finding has at least this severity",
    )

    d = sub.add_parser("drift", help="analyse drift from `terraform plan -refresh-only`")
    _common(d)
    d.add_argument("--github-issue", action="store_true", help="create/update/close a drift issue")
    d.add_argument("--exit-code", action="store_true", help=f"exit {EXIT_DRIFT} when actionable drift is found")
    return parser


def _emit(text: str, output: str) -> None:
    if output:
        Path(output).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)


def _gh_output(**values: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            for k, v in values.items():
                f.write(f"{k}={v}\n")


def _gh_summary(markdown: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(markdown + "\n\n")


def _cmd_review(args: argparse.Namespace, plan, client) -> int:
    report = run_review(plan, client)
    md = render_review_md(report, args.name)
    _emit(md if args.format == "markdown" else review_json(report), args.output)
    _gh_summary(md)
    _gh_output(**{"max-severity": report.max_severity, "findings-count": str(len(report.findings))})

    if args.github_comment:
        pr = args.pr or pr_number_from_event()
        if not pr:
            print("tfscout: --github-comment needs a pull_request event or --pr", file=sys.stderr)
        else:
            try:
                url = GitHub.from_env().upsert_pr_comment(pr, review_marker(args.name or "default"), md)
                print(f"tfscout: PR comment: {url}", file=sys.stderr)
            except GitHubError as e:
                print(f"tfscout: failed to comment on PR: {e}", file=sys.stderr)
                return 1

    if args.fail_on != "none" and report.findings and sev_rank(report.max_severity) >= sev_rank(args.fail_on):
        print(f"tfscout: findings at or above '{args.fail_on}' severity", file=sys.stderr)
        return EXIT_FAIL_ON
    return 0


def _cmd_drift(args: argparse.Namespace, plan, client) -> int:
    report = run_drift(plan, client)
    md = render_drift_md(report, args.name)
    _emit(md if args.format == "markdown" else drift_json(report), args.output)
    _gh_summary(md)
    has_drift = bool(report.actionable)
    _gh_output(
        **{
            "drift-detected": str(has_drift).lower(),
            "fingerprint": report.fingerprint,
            "findings-count": str(len(report.actionable)),
        }
    )

    if args.github_issue:
        title = f"Terraform drift detected{f': {args.name}' if args.name else ''}"
        try:
            action, url = GitHub.from_env().sync_drift_issue(
                drift_marker(args.name or "default"), title, md, report.fingerprint
            )
            print(f"tfscout: drift issue {action}{f': {url}' if url else ''}", file=sys.stderr)
        except GitHubError as e:
            print(f"tfscout: failed to sync drift issue: {e}", file=sys.stderr)
            return 1

    return EXIT_DRIFT if args.exit_code and has_drift else 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        plan = load_plan(args.plan, args.terraform_bin, args.chdir)
    except PlanError as e:
        print(f"tfscout: error: {e}", file=sys.stderr)
        return 1
    try:
        client = make_client(
            args.provider,
            args.model,
            args.base_url,
            args.api_key,
            timeout=args.timeout,
            reasoning_effort=args.reasoning_effort,
        )
    except LLMError as e:
        # e.g. secrets are not available to pull requests from forks: still produce the rules-only report
        print(f"tfscout: warning: AI disabled: {e}", file=sys.stderr)
        client = None
    if args.command == "review":
        return _cmd_review(args, plan, client)
    return _cmd_drift(args, plan, client)
