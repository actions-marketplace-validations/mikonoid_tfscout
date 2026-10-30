"""Loading and normalising `terraform show -json` plan output."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class PlanError(Exception):
    pass


@dataclass
class Change:
    address: str
    type: str
    name: str
    provider: str
    actions: list[str]
    before: Any = None
    after: Any = None
    before_sensitive: Any = None
    after_sensitive: Any = None
    after_unknown: Any = None
    replace_paths: list[str] = field(default_factory=list)
    mode: str = "managed"

    @property
    def action(self) -> str:
        acts = self.actions
        if "delete" in acts and "create" in acts:
            return "replace"
        return acts[0] if acts else "no-op"


@dataclass
class Plan:
    terraform_version: str = ""
    changes: list[Change] = field(default_factory=list)
    drift: list[Change] = field(default_factory=list)


def _to_change(entry: dict[str, Any]) -> Change:
    ch = entry.get("change") or {}
    return Change(
        address=entry.get("address", "?"),
        type=entry.get("type", "?"),
        name=entry.get("name", "?"),
        provider=entry.get("provider_name", ""),
        mode=entry.get("mode", "managed"),
        actions=list(ch.get("actions") or []),
        before=ch.get("before"),
        after=ch.get("after"),
        before_sensitive=ch.get("before_sensitive"),
        after_sensitive=ch.get("after_sensitive"),
        after_unknown=ch.get("after_unknown"),
        replace_paths=[
            ".".join(str(p) for p in path) for path in ch.get("replace_paths") or [] if isinstance(path, list)
        ],
    )


def _is_actionable(c: Change) -> bool:
    return c.mode == "managed" and c.action not in ("no-op", "read")


def parse_plan(data: dict[str, Any]) -> Plan:
    if not isinstance(data, dict) or "format_version" not in data:
        raise PlanError("input is not a `terraform show -json` plan document")
    return Plan(
        terraform_version=data.get("terraform_version", ""),
        changes=[c for c in map(_to_change, data.get("resource_changes") or []) if _is_actionable(c)],
        drift=[c for c in map(_to_change, data.get("resource_drift") or []) if _is_actionable(c)],
    )


def load_plan(path: str | Path, terraform_bin: str = "terraform", chdir: str | None = None) -> Plan:
    """Load a plan from a JSON file, or from a binary plan file via `terraform show -json`."""
    p = Path(path)
    if not p.is_file():
        raise PlanError(f"plan file not found: {p}")
    raw = p.read_bytes()
    try:
        return parse_plan(_extract_json(raw.decode("utf-8")))
    except (ValueError, UnicodeDecodeError):
        pass
    try:
        proc = subprocess.run(
            [terraform_bin, "show", "-json", str(p.resolve())],
            cwd=chdir,
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as e:
        raise PlanError(f"`{terraform_bin}` not found; pass a JSON plan or set --terraform-bin") from e
    except subprocess.CalledProcessError as e:
        raise PlanError(f"`{terraform_bin} show -json` failed: {e.stderr.strip()}") from e
    try:
        return parse_plan(_extract_json(proc.stdout))
    except ValueError as e:
        raise PlanError(f"cannot parse `{terraform_bin} show -json` output: {e}") from e


def _extract_json(text: str) -> Any:
    """Parse JSON, tolerating extra lines such as those added by the hashicorp/setup-terraform wrapper."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r'\{\s*"format_version"', text)
        if not m:
            raise
        obj, _ = json.JSONDecoder().raw_decode(text, m.start())
        return obj
