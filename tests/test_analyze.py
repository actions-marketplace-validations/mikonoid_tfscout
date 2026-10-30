from tfscout.analyze import run_drift, run_review
from tfscout.llm import LLMError
from tfscout.plan import Plan
from tfscout.redact import REDACTED
from tfscout.render import render_drift_md, render_review_md


def test_review_without_llm(review_plan):
    r = run_review(review_plan, None)
    assert r.counts == {"create": 2, "update": 2, "replace": 1, "delete": 0}
    assert r.max_severity == "critical"
    assert not r.ai_used


def test_review_never_sends_secrets(review_plan, fake_llm):
    llm = fake_llm({"summary": "- ok", "findings": []})
    run_review(review_plan, llm)
    sent = llm.calls[0][1]
    assert "SuperSecret123!" not in sent
    assert "AKIAABCDEFGHIJKLMNOP" not in sent
    assert REDACTED in sent


def test_review_merges_ai_findings(review_plan, fake_llm):
    llm = fake_llm(
        {
            "summary": "- Recreates the orders DB",
            "findings": [
                {
                    "severity": "high",
                    "category": "availability",
                    "resource": "aws_db_instance.main",
                    "title": "AZ change forces replacement",
                    "detail": "availability_zone",
                    "recommendation": "keep AZ",
                },
                {
                    "severity": "critical",
                    "category": "data-loss",
                    "resource": "aws_db_instance.main",
                    "title": "duplicate of rule",
                },
                {"severity": "bogus", "category": "weird", "resource": "aws_instance.app", "title": "Bigger instance"},
                {"no": "title"},
            ],
        }
    )
    r = run_review(review_plan, llm)
    ai = [f for f in r.findings if f.source == "ai"]
    assert {f.title for f in ai} == {"AZ change forces replacement", "Bigger instance"}
    bigger = next(f for f in ai if f.title == "Bigger instance")
    assert (bigger.severity, bigger.category) == ("info", "other")
    assert r.summary.startswith("- Recreates")


def test_review_falls_back_when_llm_fails(review_plan, fake_llm):
    r = run_review(review_plan, fake_llm(error=LLMError("boom")))
    assert r.ai_error == "boom" and not r.ai_used
    assert r.findings
    assert "AI analysis unavailable" in render_review_md(r)


def test_review_markdown(review_plan):
    md = render_review_md(run_review(review_plan, None), "prod")
    assert md.startswith("<!-- tfscout:review:prod -->")
    assert "🔴" in md and "aws_db_instance.main" in md
    assert "SuperSecret123!" not in md


def test_drift_ai_cannot_downgrade_risky(drift_plan, fake_llm):
    llm = fake_llm(
        {
            "summary": "- drift",
            "items": [
                {"resource": "aws_security_group.web", "classification": "noise", "explanation": "x"},
                {
                    "resource": "aws_instance.app",
                    "classification": "risky",
                    "explanation": "upsized",
                    "recommendation": 'set instance_type = "t3.large"',
                },
                {"resource": "unknown.res", "classification": "risky"},
            ],
        }
    )
    r = run_drift(drift_plan, llm)
    by = {i.resource: i for i in r.items}
    assert by["aws_security_group.web"].classification == "risky"
    assert by["aws_instance.app"].classification == "risky"
    assert by["aws_instance.app"].recommendation.startswith("set instance_type")
    assert "unknown.res" not in by


def test_drift_fingerprint_stable_and_ignores_noise(drift_plan):
    a = run_drift(drift_plan, None)
    b = run_drift(drift_plan, None)
    assert a.fingerprint and a.fingerprint == b.fingerprint
    assert len(a.actionable) == 3
    noise_only = Plan(drift=[c for c in drift_plan.drift if c.address == "aws_ecs_service.api"])
    assert run_drift(noise_only, None).fingerprint == ""


def test_drift_markdown(drift_plan):
    md = render_drift_md(run_drift(drift_plan, None), "qa")
    assert md.startswith("<!-- tfscout:drift:qa -->")
    assert "`ingress[-]`" in md and "`ingress[+]`" in md
    assert "10.0.0.0/8" in md and "0.0.0.0/0" in md
    assert "fingerprint" in md


def test_no_changes():
    assert "No changes" in render_review_md(run_review(Plan(), None))
    assert "No drift" in render_drift_md(run_drift(Plan(), None))


def test_drift_ai_cannot_override_proven_noise(fake_llm):
    from tfscout.plan import Change, Resource

    role = Change(
        "aws_iam_role.ci",
        "aws_iam_role",
        "ci",
        "",
        ["update"],
        before={"name": "ci", "managed_policy_arns": []},
        after={"name": "ci", "managed_policy_arns": ["arn:p"]},
    )
    empty = Change("aws_s3_bucket.b", "aws_s3_bucket", "b", "", ["update"], before={"x": None}, after={"x": ""})
    state = [Resource("a", "aws_iam_role_policy_attachment", {"role": "ci", "policy_arn": "arn:p"})]
    llm = fake_llm({"items": [{"resource": "aws_iam_role.ci", "classification": "manual-change"}]})
    r = run_drift(Plan(drift=[role, empty], prior_resources=state), llm)
    assert [(i.resource, i.classification) for i in r.items] == [("aws_iam_role.ci", "noise")]
    assert '"classification_is_final": true' in llm.calls[0][1]
