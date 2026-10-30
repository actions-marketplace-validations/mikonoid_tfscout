"""Attribute-level diff between `before` and `after` of a resource change."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

UNKNOWN = "(known after apply)"
_MISSING = object()


@dataclass(frozen=True)
class AttrDiff:
    path: str
    before: Any
    after: Any


def _flatten(value: Any, prefix: str, out: dict[str, Any]) -> None:
    if isinstance(value, dict) and value:
        for k, v in value.items():
            _flatten(v, f"{prefix}.{k}" if prefix else str(k), out)
    elif isinstance(value, list) and value and any(isinstance(v, (dict, list)) for v in value):
        for i, v in enumerate(value):
            _flatten(v, f"{prefix}[{i}]", out)
    else:
        out[prefix] = value


def flatten(value: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if value is not None:
        _flatten(value, "", out)
    return out


def _unknown_paths(after_unknown: Any) -> set[str]:
    return {p for p, v in flatten(after_unknown).items() if v is True}


def _short(v: Any, limit: int) -> Any:
    if isinstance(v, (dict, list)):
        v = json.dumps(v, sort_keys=True)
    if isinstance(v, str) and len(v) > limit:
        return v[:limit] + f"...(+{len(v) - limit} chars)"
    return v


def diff(before: Any, after: Any, after_unknown: Any = None, value_limit: int = 200) -> list[AttrDiff]:
    b, a = flatten(before), flatten(after)
    unknown = _unknown_paths(after_unknown)
    result: list[AttrDiff] = []
    for path in sorted(set(b) | set(a) | unknown):
        bv = b.get(path, _MISSING)
        av = UNKNOWN if path in unknown else a.get(path, _MISSING)
        if bv == av:
            continue
        # Treat null / empty / missing as equivalent to avoid noisy diffs
        if bv in (_MISSING, None, "", [], {}) and av in (_MISSING, None, "", [], {}):
            continue
        result.append(
            AttrDiff(
                path=path,
                before=None if bv is _MISSING else _short(bv, value_limit),
                after=None if av is _MISSING else _short(av, value_limit),
            )
        )
    return result
