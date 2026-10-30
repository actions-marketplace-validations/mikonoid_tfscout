import io
import json
import urllib.error

import pytest

from tfscout import llm
from tfscout.llm import LLMClient, LLMError, make_client, parse_json_content


def test_parse_json_plain_and_fenced():
    assert parse_json_content('{"a": 1}') == {"a": 1}
    assert parse_json_content('```json\n{"a": 2}\n```') == {"a": 2}
    assert parse_json_content('Sure! {"a": 3} hope it helps') == {"a": 3}


@pytest.mark.parametrize("bad", ["", "no json", "[1,2]", "{broken"])
def test_parse_json_errors(bad):
    with pytest.raises(LLMError):
        parse_json_content(bad)


def test_make_client_presets():
    assert make_client("none") is None
    c = make_client("gemini", env={"GEMINI_API_KEY": "g"})
    assert "generativelanguage" in c.base_url and c.api_key == "g"
    c = make_client("ollama", env={})
    assert c.api_key == "" and "11434" in c.base_url
    c = make_client("openai", model="gpt-x", env={"TFSCOUT_API_KEY": "k"})
    assert c.model == "gpt-x" and c.api_key == "k"


def test_make_client_errors():
    with pytest.raises(LLMError, match="API key"):
        make_client("openai", env={})
    with pytest.raises(LLMError, match="custom"):
        make_client("custom", env={})
    with pytest.raises(LLMError, match="unknown provider"):
        make_client("nope", env={})


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_client_request_and_retry(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout):
        calls.append(req)
        if len(calls) == 1:
            raise urllib.error.HTTPError(req.full_url, 429, "rate", {}, io.BytesIO(b"slow down"))
        body = {"choices": [{"message": {"content": '{"summary": "ok"}'}}]}
        return _Resp(json.dumps(body).encode())

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    out = LLMClient("https://x/v1/", "m", "key").complete_json("sys", "user")
    assert out == {"summary": "ok"}
    assert calls[-1].full_url == "https://x/v1/chat/completions"
    assert calls[-1].get_header("Authorization") == "Bearer key"
    sent = json.loads(calls[-1].data)
    assert sent["response_format"] == {"type": "json_object"} and sent["model"] == "m"


def test_client_non_json_body(monkeypatch):
    monkeypatch.setattr(llm.urllib.request, "urlopen", lambda req, timeout: _Resp(b"OK"))
    with pytest.raises(LLMError, match="non-JSON"):
        LLMClient("https://x", "m").complete_json("s", "u")


def test_client_no_retry_on_401(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout):
        calls.append(req)
        raise urllib.error.HTTPError(req.full_url, 401, "no", {}, io.BytesIO(b"bad key"))

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(LLMError, match="401"):
        LLMClient("https://x", "m").complete_json("s", "u")
    assert len(calls) == 1


def test_404_lists_available_models(monkeypatch):
    def fake_urlopen(req, timeout):
        if req.full_url.endswith("/models"):
            body = {"data": [{"id": "models/gemini-3.8-flash"}, {"id": "models/gemini-3.8-pro"}]}
            return _Resp(json.dumps(body).encode())
        raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, io.BytesIO(b"model gone"))

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(LLMError, match=r"--model.*Available: gemini-3.8-flash, gemini-3.8-pro"):
        LLMClient("https://x", "old-model").complete_json("s", "u")
