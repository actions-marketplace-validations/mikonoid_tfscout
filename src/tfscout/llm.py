"""Minimal OpenAI-compatible chat client (stdlib only).

Gemini, OpenAI, Groq, OpenRouter, Ollama, Azure AI Foundry (v1), LM Studio and vLLM
all speak the `/chat/completions` dialect, so one client covers them.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

PRESETS: dict[str, dict[str, str]] = {
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "model": "gemini-flash-lite-latest,gemini-3.5-flash,gemini-flash-latest",
        "key_env": "GEMINI_API_KEY",
        # Gemini 3 models always think; "low" keeps latency reasonable for CI.
        "reasoning_effort": "low",
        # Google recommends the default temperature (1.0) for Gemini 3; lower values can cause looping.
        "temperature": "default",
    },
    "openai": {"base_url": "https://api.openai.com/v1", "model": "gpt-6-luna", "key_env": "OPENAI_API_KEY"},
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "model": "llama-3.3-70b-versatile",
        "key_env": "GROQ_API_KEY",
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "model": "openai/gpt-6-luna",
        "key_env": "OPENROUTER_API_KEY",
    },
    "ollama": {"base_url": "http://localhost:11434/v1", "model": "qwen2.5-coder:7b", "key_env": ""},
}
PROVIDERS = ["none", *PRESETS, "custom"]


class LLMError(Exception):
    pass


def _log(msg: str) -> None:
    print(f"tfscout: {msg}", file=sys.stderr, flush=True)


@dataclass
class LLMClient:
    base_url: str
    model: str
    api_key: str = ""
    timeout: float = 90.0
    retries: int = 3
    reasoning_effort: str = ""
    temperature: float | None = 0.1
    max_timeouts: int = 2

    @property
    def models(self) -> list[str]:
        """`model` may be a comma-separated fallback chain."""
        return [m.strip() for m in self.model.split(",") if m.strip()] or [self.model]

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = self.base_url.rstrip("/") + "/chat/completions"
        headers = {"Content-Type": "application/json", "User-Agent": "tfscout"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        last: Exception | None = None
        timeouts = 0
        models = self.models
        schedule = [models[i % len(models)] for i in range(max(self.retries, len(models)))]
        for attempt, model in enumerate(schedule):
            nxt = schedule[attempt + 1] if attempt + 1 < len(schedule) else None
            _log(f"asking {model} (attempt {attempt + 1}/{len(schedule)}, timeout {self.timeout:.0f}s)...")
            started = time.monotonic()
            data = json.dumps({**payload, "model": model}).encode()
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                _log(f"AI response from {model} in {time.monotonic() - started:.1f}s")
                try:
                    return json.loads(raw)
                except json.JSONDecodeError as e:
                    raise LLMError(f"non-JSON response from {url}: {raw[:120]!r}") from e
            except urllib.error.HTTPError as e:
                body = e.read().decode(errors="replace")[:500]
                last = LLMError(f"HTTP {e.code} from {url} ({model}): {body}")
                if e.code == 404:
                    if nxt is None or nxt == model:
                        last = LLMError(f"{last} {self._model_hint(model)}")
                        break
                elif e.code not in (408, 429, 500, 502, 503, 504):
                    break
            except (TimeoutError, urllib.error.URLError) as e:
                if isinstance(e, TimeoutError) or isinstance(getattr(e, "reason", None), TimeoutError):
                    timeouts += 1
                    last = LLMError(f"no response from {model} within {self.timeout:.0f}s (use --timeout)")
                    if len(models) == 1 and timeouts >= self.max_timeouts:
                        break
                else:
                    last = LLMError(f"cannot reach {url}: {e}")
            if nxt is not None:
                if nxt == model:
                    _log(f"retrying after: {last}")
                    time.sleep(2**attempt)
                else:
                    _log(f"{model} failed ({str(last)[:120]}); falling back to {nxt}")
        raise last or LLMError("unknown error")

    def list_models(self) -> list[str]:
        """Model ids from the OpenAI-compatible `GET /models` endpoint (empty list on failure)."""
        req = urllib.request.Request(self.base_url.rstrip("/") + "/models", headers={"User-Agent": "tfscout"})
        if self.api_key:
            req.add_header("Authorization", "Bearer " + self.api_key)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError):
            return []
        items = data.get("data") if isinstance(data, dict) else None
        ids = [str(m.get("id", "")) for m in items or [] if isinstance(m, dict)]
        return sorted(i.removeprefix("models/") for i in ids if i)

    def _model_hint(self, model: str) -> str:
        hint = f"Model '{model}' may be unavailable; set --model or TFSCOUT_MODEL (comma-separated for fallbacks)."
        models = self.list_models()
        if models:
            shown = ", ".join(models[:20]) + (f" (+{len(models) - 20} more)" if len(models) > 20 else "")
            hint += f" Available: {shown}"
        return hint

    def complete_json(self, system: str, user: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        resp = self._post(payload)
        try:
            content = resp["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"unexpected response shape: {str(resp)[:300]}") from e
        return parse_json_content(content)


def parse_json_content(content: str) -> dict[str, Any]:
    text = (content or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise LLMError("model did not return JSON")
    try:
        obj = json.loads(text[start : end + 1])
    except json.JSONDecodeError as e:
        raise LLMError(f"model returned invalid JSON: {e}") from e
    if not isinstance(obj, dict):
        raise LLMError("model returned non-object JSON")
    return obj


def make_client(
    provider: str,
    model: str = "",
    base_url: str = "",
    api_key: str = "",
    env: dict[str, str] | None = None,
    timeout: float = 90.0,
    reasoning_effort: str = "",
) -> LLMClient | None:
    env = os.environ if env is None else env
    if provider == "none":
        return None
    if provider == "custom":
        if not base_url or not model:
            raise LLMError("provider 'custom' requires --base-url and --model")
        return LLMClient(
            base_url,
            model,
            api_key or env.get("TFSCOUT_API_KEY", ""),
            timeout=timeout,
            reasoning_effort=reasoning_effort,
        )
    if provider not in PRESETS:
        raise LLMError(f"unknown provider '{provider}', choose one of: {', '.join(PROVIDERS)}")
    p = PRESETS[provider]
    key = api_key or env.get("TFSCOUT_API_KEY", "") or (env.get(p["key_env"], "") if p["key_env"] else "")
    if p["key_env"] and not key:
        raise LLMError(f"provider '{provider}' needs an API key (--api-key, TFSCOUT_API_KEY or {p['key_env']})")
    return LLMClient(
        base_url or p["base_url"],
        model or p["model"],
        key,
        timeout=timeout,
        reasoning_effort=reasoning_effort or p.get("reasoning_effort", ""),
        temperature=None if p.get("temperature") == "default" else 0.1,
    )
