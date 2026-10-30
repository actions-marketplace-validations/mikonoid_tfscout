import json
from pathlib import Path

import pytest

from tfscout.plan import parse_plan

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def review_plan():
    return parse_plan(json.loads((FIXTURES / "plan_review.json").read_text()))


@pytest.fixture
def drift_plan():
    return parse_plan(json.loads((FIXTURES / "plan_drift.json").read_text()))


class FakeLLM:
    """Records prompts and returns a canned response (or raises)."""

    def __init__(self, response=None, error=None):
        self.response = response or {}
        self.error = error
        self.calls = []

    def complete_json(self, system, user):
        self.calls.append((system, user))
        if self.error:
            raise self.error
        return self.response


@pytest.fixture
def fake_llm():
    return FakeLLM
