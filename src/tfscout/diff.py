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


def _is_object_list(v: Any) -> bool:
    return isinstance(v, list) and bool(v) and all(isinstance(x, dict) for x in v)


def _canon(v: Any) -> str:
    return json.dumps(v, sort_keys=True, default=str)


def _split_object_lists(before: Any, after: Any, prefix: str, extra: list[AttrDiff], sets: set[str]) -> tuple:
    """Diff lists of objects (e.g. SG ingress, nested blocks) as sets, not by index.

    Matching elements are dropped, the rest are reported as `path[-]` (removed) / `path[+]` (added),
    so adding one rule doesn't show up as every rule after it "changing".
    """
    if not (isinstance(before, dict) and isinstance(after, dict)):
        return before, after
    nb: dict[str, Any] = {}
    na: dict[str, Any] = {}
    for k in before.keys() | after.keys():
        path = f"{prefix}.{k}" if prefix else str(k)
        vb, va = before.get(k, _MISSING), after.get(k, _MISSING)
        sides_ok = all(x is _MISSING or x is None or isinstance(x, list) for x in (vb, va))
        if sides_ok and (_is_object_list(vb) or _is_object_list(va)):
            sets.add(path)
            lb = vb if isinstance(vb, list) else []
            la = va if isinstance(va, list) else []
            kb, ka = [_canon(x) for x in lb], [_canon(x) for x in la]
            extra += [AttrDiff(f"{path}[-]", x, None) for x, c in zip(lb, kb, strict=True) if c not in ka]
            extra += [AttrDiff(f"{path}[+]", None, x) for x, c in zip(la, ka, strict=True) if c not in kb]
            continue
        rb, ra = _split_object_lists(vb, va, path, extra, sets)
        if vb is not _MISSING:
            nb[k] = rb
        if va is not _MISSING:
            na[k] = ra
    return nb, na


def diff(before: Any, after: Any, after_unknown: Any = None, value_limit: int = 200) -> list[AttrDiff]:
    extra: list[AttrDiff] = []
    sets: set[str] = set()
    before, after = _split_object_lists(before, after, "", extra, sets)
    b, a = flatten(before), flatten(after)
    unknown = {p for p in _unknown_paths(after_unknown) if not any(p.startswith(s + "[") for s in sets)}
    result: list[AttrDiff] = [
        AttrDiff(d.path, _short(d.before, value_limit), _short(d.after, value_limit)) for d in extra
    ]
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
    result.sort(key=lambda d: d.path)
    return result
