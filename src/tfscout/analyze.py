"""Orchestration: plan -> redaction -> rules -> (optional) LLM -> report."""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass, field
from typing import Any

from tfscout.diff import diff
from tfscout.llm import LLMClient, LLMError
from tfscout.plan import Change, Plan
from tfscout.redact import redact_change
from tfscout.rules import (
    DRIFT_CLASSES,
    SEVERITIES,
    DriftItem,
    Finding,
    drift_rules,
    review_rules,
    sev_rank,
)

MAX_RESOURCES = 150
MAX_DIFFS_PER_RESOURCE = 40
CATEGORIES = {"security", "data-loss", "availability", "cost", "compliance", "other"}

REVIEW_SYSTEM = """\
You are tfscout, a senior platform engineer reviewing a Terraform plan attached to a pull request.
Input: resource changes (secrets are already redacted as [REDACTED]) and findings produced by deterministic rules.

Tasks:
1. "summary": 2-5 short markdown bullet lines describing WHAT this change does and its blast radius, in plain English.
2. "findings": ONLY risks the rules did not already report: downtime, data loss, security, cost,
   compliance, surprising side-effects (e.g. a renamed resource causing destroy/create, forced replacement
   attributes, changes to shared infrastructure). Never repeat a rule finding. Never invent problems:
   if nothing else is notable return an empty list. Be concrete and reference attribute paths.

Respond with a single JSON object and nothing else:
{"summary": "- ...\\n- ...", "findings": [{"severity": "info|low|medium|high|critical",
 "category": "security|data-loss|availability|cost|compliance|other", "resource": "<resource address>",
 "title": "<short>", "detail": "<why>", "recommendation": "<what to do>"}]}
"""

DRIFT_SYSTEM = """\
You are tfscout, a senior platform engineer analysing Terraform DRIFT: differences between the Terraform state
and real infrastructure that were made outside Terraform. Secrets are redacted as [REDACTED].
Each item has a preliminary classification from rules: noise | manual-change | risky.

For every item return:
- "classification": noise (volatile/auto-managed values), manual-change (someone changed it by hand,
  probably a hotfix that should be codified), or risky (weakens security, availability or compliance,
  or looks unauthorised).
- "explanation": 1-2 sentences on what changed and the likely cause.
- "recommendation": either codify the change in Terraform (show the HCL attribute to set) or revert it by
  running apply, or ignore it via lifecycle.ignore_changes for true noise.
Also give an overall "summary" (2-4 markdown bullet lines).

Respond with a single JSON object and nothing else:
{"summary": "- ...", "items": [{"resource": "<address>", "classification": "noise|manual-change|risky",
 "explanation": "...", "recommendation": "..."}]}
"""


def _warn(msg: str) -> None:
    print(f"tfscout: warning: {msg}", file=sys.stderr)


def _diff_payload(c: Change) -> list[dict[str, Any]]:
    ds = diff(c.before, c.after, c.after_unknown)
    out = [{"path": d.path, "before": d.before, "after": d.after} for d in ds[:MAX_DIFFS_PER_RESOURCE]]
    if len(ds) > MAX_DIFFS_PER_RESOURCE:
        out.append({"path": f"... {len(ds) - MAX_DIFFS_PER_RESOURCE} more attributes omitted"})
    return out


def _finding_dict(f: Finding) -> dict[str, str]:
    return {"severity": f.severity, "category": f.category, "resource": f.resource, "title": f.title}


# --------------------------------------------------------------------------- review


@dataclass
class ReviewReport:
    summary: str
    counts: dict[str, int]
    findings: list[Finding]
    changes: list[Change]
    ai_used: bool = False
    ai_error: str = ""

    @property
    def max_severity(self) -> str:
        return max((f.severity for f in self.findings), key=sev_rank, default="none")


def _count_actions(changes: list[Change]) -> dict[str, int]:
    counts = {"create": 0, "update": 0, "replace": 0, "delete": 0}
    for c in changes:
        counts[c.action] = counts.get(c.action, 0) + 1
    return counts


def _parse_ai_findings(raw: Any, known: set[str]) -> list[Finding]:
    out = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict) or not item.get("title"):
            continue
        sev = str(item.get("severity", "info")).lower()
        cat = str(item.get("category", "other")).lower()
        res = str(item.get("resource", ""))
        out.append(
            Finding(
                severity=sev if sev in SEVERITIES else "info",
                category=cat if cat in CATEGORIES else "other",
                resource=res if res in known else (res or "(plan)"),
                title=str(item["title"])[:200],
                detail=str(item.get("detail", ""))[:1000],
                recommendation=str(item.get("recommendation", ""))[:1000],
                source="ai",
            )
        )
    return out


def run_review(plan: Plan, client: LLMClient | None) -> ReviewReport:
    changes = [redact_change(c) for c in plan.changes]
    findings = review_rules(changes)
    report = ReviewReport(summary="", counts=_count_actions(changes), findings=findings, changes=changes)
    if not changes or client is None:
        return report

    payload = {
        "terraform_version": plan.terraform_version,
        "counts": report.counts,
        "changes": [
            {
                "address": c.address,
                "type": c.type,
                "action": c.action,
                "replace_paths": c.replace_paths,
                "diff": _diff_payload(c),
            }
            for c in changes[:MAX_RESOURCES]
        ],
        "omitted_changes": max(0, len(changes) - MAX_RESOURCES),
        "rule_findings": [_finding_dict(f) for f in findings],
    }
    try:
        resp = client.complete_json(REVIEW_SYSTEM, json.dumps(payload, default=str))
    except LLMError as e:
        _warn(f"AI analysis skipped: {e}")
        report.ai_error = str(e)
        return report

    report.ai_used = True
    report.summary = str(resp.get("summary", "")).strip()
    seen = {(f.resource, f.category) for f in findings}
    for f in _parse_ai_findings(resp.get("findings"), {c.address for c in changes}):
        if (f.resource, f.category) not in seen:
            report.findings.append(f)
            seen.add((f.resource, f.category))
    report.findings.sort(key=lambda f: (-sev_rank(f.severity), f.source != "rule", f.resource))
    return report


# --------------------------------------------------------------------------- drift


@dataclass
class DriftReport:
    items: list[DriftItem] = field(default_factory=list)
    summary: str = ""
    ai_used: bool = False
    ai_error: str = ""

    @property
    def actionable(self) -> list[DriftItem]:
        return [i for i in self.items if i.classification != "noise"]

    @property
    def fingerprint(self) -> str:
        """Stable id of the actionable drift, so repeated runs don't spam new issues."""
        key = sorted(
            (i.resource, i.action, sorted((d.path, json.dumps(d.after, default=str)) for d in i.diffs))
            for i in self.actionable
        )
        return hashlib.sha256(json.dumps(key).encode()).hexdigest()[:12] if key else ""


def run_drift(plan: Plan, client: LLMClient | None) -> DriftReport:
    report = DriftReport(items=drift_rules([redact_change(c) for c in plan.drift]))
    if not report.items or client is None:
        return report

    payload = {
        "items": [
            {
                "resource": i.resource,
                "type": i.type,
                "action": i.action,
                "preliminary_classification": i.classification,
                "reason": i.reason,
                "diff": [{"path": d.path, "state": d.before, "real": d.after} for d in i.diffs][
                    :MAX_DIFFS_PER_RESOURCE
                ],
            }
            for i in report.items[:MAX_RESOURCES]
        ]
    }
    try:
        resp = client.complete_json(DRIFT_SYSTEM, json.dumps(payload, default=str))
    except LLMError as e:
        _warn(f"AI analysis skipped: {e}")
        report.ai_error = str(e)
        return report

    report.ai_used = True
    report.summary = str(resp.get("summary", "")).strip()
    by_res = {i.resource: i for i in report.items}
    for ai in resp.get("items") or []:
        if not isinstance(ai, dict) or ai.get("resource") not in by_res:
            continue
        item = by_res[ai["resource"]]
        cls = str(ai.get("classification", "")).lower()
        # Rules are security-first: the model may escalate but never downgrade a risky item.
        if cls in DRIFT_CLASSES and item.classification != "risky":
            item.classification = cls
        item.explanation = str(ai.get("explanation", ""))[:1000]
        item.recommendation = str(ai.get("recommendation", ""))[:1500]
    order = {k: i for i, k in enumerate(reversed(DRIFT_CLASSES))}
    report.items.sort(key=lambda i: (order[i.classification], i.resource))
    return report
