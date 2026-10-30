from tfscout.diff import UNKNOWN, diff
from tfscout.plan import Change
from tfscout.redact import redact_change
from tfscout.rules import classify_drift, review_rules


def _findings(plan):
    return review_rules([redact_change(c) for c in plan.changes])


def test_diff_basic_and_unknown():
    d = diff({"a": 1, "b": {"c": 2}}, {"a": 1, "b": {"c": 3}}, {"id": True})
    assert [(x.path, x.before, x.after) for x in d] == [("b.c", 2, 3), ("id", None, UNKNOWN)]


def test_diff_ignores_null_vs_empty():
    assert diff({"x": None, "y": []}, {"x": "", "y": None}) == []


def test_stateful_replace_is_critical(review_plan):
    f = [x for x in _findings(review_plan) if x.resource == "aws_db_instance.main"]
    titles = {x.title for x in f}
    assert any("REPLACED" in t for t in titles)
    assert any("`availability_zone`" in x.detail for x in f)
    assert any(x.severity == "critical" and x.category == "data-loss" for x in f)
    assert any("Encryption disabled" in t for t in titles)
    assert "Deletion protection disabled" in titles


def test_new_ssh_ingress_only(review_plan):
    f = [x for x in _findings(review_plan) if x.resource == "aws_security_group.web"]
    assert len(f) == 1, "pre-existing 443 rule must not be reported"
    assert f[0].severity == "critical"
    assert "22/tcp" in f[0].title


def test_iam_star_star(review_plan):
    f = [x for x in _findings(review_plan) if x.resource == "aws_iam_policy.ci"]
    assert f and f[0].severity == "critical"


def test_benign_update_has_no_findings(review_plan):
    assert not [x for x in _findings(review_plan) if x.resource == "aws_instance.app"]


def test_findings_sorted_by_severity(review_plan):
    sev = [x.severity for x in _findings(review_plan)]
    order = ["critical", "high", "medium", "low", "info"]
    assert sev == sorted(sev, key=order.index)


def test_vpc_ingress_rule_resource():
    c = Change(
        "aws_vpc_security_group_ingress_rule.r",
        "aws_vpc_security_group_ingress_rule",
        "r",
        "",
        ["create"],
        after={"cidr_ipv4": "0.0.0.0/0", "from_port": 8080, "to_port": 8080, "ip_protocol": "tcp"},
    )
    f = review_rules([c])
    assert len(f) == 1 and f[0].severity == "high"


def test_drift_classification(drift_plan):
    by = {c.address: classify_drift(c) for c in drift_plan.drift}
    assert by["aws_security_group.web"].classification == "risky"
    assert by["aws_instance.app"].classification == "manual-change"
    assert [d.path for d in by["aws_instance.app"].diffs] == ["instance_type"]
    assert by["aws_ecs_service.api"].classification == "noise"
    assert by["aws_s3_bucket.old_logs"].classification == "risky"
