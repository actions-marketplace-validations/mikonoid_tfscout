"""Secret redaction. Nothing leaves the runner without passing through here.

`terraform show -json` prints sensitive values in clear text and only marks them in
`*_sensitive` maps, so we apply those masks plus key/value heuristics.
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

from tfscout.plan import Change

REDACTED = "[REDACTED]"

_SECRET_KEY = re.compile(
    r"(password|passwd|secret|token|private_key|api_key|apikey|access_key|credential|"
    r"connection_string|auth_string|master_password|client_secret|sas_token)",
    re.IGNORECASE,
)

_SECRET_VALUE = [
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"ASIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{30,}"),
    re.compile(r"xox[abposr]-[A-Za-z0-9-]{10,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"(?i)(postgres|mysql|mongodb(\+srv)?|redis|amqp)://[^:/\s]+:[^@\s]+@"),
]


def _apply_mask(value: Any, mask: Any) -> Any:
    if mask is True:
        return REDACTED if value is not None else None
    if isinstance(value, dict) and isinstance(mask, dict):
        return {k: _apply_mask(v, mask.get(k)) for k, v in value.items()}
    if isinstance(value, list) and isinstance(mask, list):
        return [_apply_mask(v, mask[i] if i < len(mask) else None) for i, v in enumerate(value)]
    return value


def _heuristics(value: Any, key: str = "") -> Any:
    if isinstance(value, dict):
        return {k: _heuristics(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_heuristics(v, key) for v in value]
    if isinstance(value, str) and value:
        if key and _SECRET_KEY.search(key) and value != REDACTED:
            return REDACTED
        if any(p.search(value) for p in _SECRET_VALUE):
            return REDACTED
    return value


def redact_value(value: Any, mask: Any = None) -> Any:
    return _heuristics(_apply_mask(value, mask))


def redact_change(c: Change) -> Change:
    return replace(
        c,
        before=redact_value(c.before, c.before_sensitive),
        after=redact_value(c.after, c.after_sensitive),
    )
