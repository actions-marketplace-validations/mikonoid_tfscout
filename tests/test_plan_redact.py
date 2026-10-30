import pytest

from tfscout.plan import PlanError, load_plan, parse_plan
from tfscout.redact import REDACTED, redact_change, redact_value


def test_parse_filters_noop_and_data(review_plan):
    addrs = [c.address for c in review_plan.changes]
    assert "aws_lambda_function.noop" not in addrs
    assert "data.aws_caller_identity.current" not in addrs
    assert len(addrs) == 5


def test_replace_action(review_plan):
    db = next(c for c in review_plan.changes if c.address == "aws_db_instance.main")
    assert db.action == "replace"


def test_parse_rejects_non_plan():
    with pytest.raises(PlanError):
        parse_plan({"foo": "bar"})


def test_load_missing_file(tmp_path):
    with pytest.raises(PlanError):
        load_plan(tmp_path / "nope.json")


def test_load_binary_without_terraform(tmp_path):
    f = tmp_path / "plan.out"
    f.write_bytes(b"PK\x03\x04binary")
    with pytest.raises(PlanError, match="not found"):
        load_plan(f, terraform_bin="definitely-not-terraform")


def test_sensitive_mask_redacts(review_plan):
    db = redact_change(next(c for c in review_plan.changes if c.address == "aws_db_instance.main"))
    assert db.before["password"] == REDACTED
    assert db.after["password"] == REDACTED
    assert db.after["username"] == "app"


def test_value_heuristics_catch_unmarked_secrets(review_plan):
    p = redact_change(next(c for c in review_plan.changes if c.address == "aws_ssm_parameter.api"))
    assert p.after["value"] == REDACTED
    assert p.after["name"] == "/app/api"


@pytest.mark.parametrize(
    "value",
    [
        "-----BEGIN RSA PRIVATE KEY-----\nabc",
        "ghp_" + "a" * 36,
        "postgres://user:pa55@db.internal:5432/app",
        "sk-" + "x" * 30,
    ],
)
def test_secret_patterns(value):
    assert redact_value({"x": value})["x"] == REDACTED


def test_secret_key_names_nested():
    v = redact_value({"env": [{"DB_PASSWORD": "hunter2", "PORT": "5432"}], "client_secret": "s"})
    assert v["env"][0]["DB_PASSWORD"] == REDACTED
    assert v["env"][0]["PORT"] == "5432"
    assert v["client_secret"] == REDACTED
