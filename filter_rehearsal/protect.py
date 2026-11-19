"""Must-keep rules: events that no filter change is allowed to drop.

protect.json is a list of objects. Every key in one object must match
(AND); an event is protected if any object matches (OR)::

    [
      {"label": "payments",   "title": "*PaymentDeclined*"},
      {"label": "enterprise", "tag": {"customer.tier": "enterprise"}},
      {"label": "fatal",      "level": "fatal"},
      {"label": "issue 4242", "issue": "4242"}
    ]

Text values are case-insensitive globs (``*`` / ``?``), like the filters.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .events import Event
from .filters import ConfigError
from .globs import GlobError, compile_glob, glob_match

KEYS = {"label", "title", "message", "error_type", "release", "level", "issue", "event_id", "tag", "transaction"}


class Protection:
    def __init__(self, label: str, checks: List[Callable[[Event], bool]]):
        self.label = label
        self.checks = checks

    def matches(self, ev: Event) -> bool:
        return all(c(ev) for c in self.checks)


def _glob(pat: str, where: str) -> str:
    try:
        compile_glob(pat)
    except GlobError as exc:
        raise ConfigError(f"{where}: {exc}") from None
    return pat


def _any(pat: str, values: List[Optional[str]]) -> bool:
    return any(v is not None and glob_match(pat, v) for v in values)


def protection_from_obj(obj: Any, where: str) -> Protection:
    if not isinstance(obj, dict):
        raise ConfigError(f"{where}: each must-keep rule is an object")
    unknown = set(obj) - KEYS
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {sorted(unknown)}; use {sorted(KEYS - {'label'})}")
    checks: List[Callable[[Event], bool]] = []
    for key, val in obj.items():
        if key == "label":
            continue
        if key == "tag":
            if not isinstance(val, dict) or not val:
                raise ConfigError(f"{where}: tag must be an object of tag -> glob")
            pairs: Dict[str, str] = {str(k): _glob(str(v), where) for k, v in val.items()}
            checks.append(lambda ev, pairs=pairs: all(k in ev.tags and glob_match(p, ev.tags[k]) for k, p in pairs.items()))
            continue
        if not isinstance(val, str):
            raise ConfigError(f"{where}: {key} must be a string")
        pat = _glob(val, where)
        if key == "title":
            checks.append(lambda ev, p=pat: glob_match(p, ev.title))
        elif key == "message":
            checks.append(lambda ev, p=pat: _any(p, ev.error_descriptions()))
        elif key == "error_type":
            checks.append(lambda ev, p=pat: _any(p, [t for t, _ in ev.exceptions]))
        elif key == "release":
            checks.append(lambda ev, p=pat: _any(p, [ev.release]))
        elif key == "level":
            checks.append(lambda ev, p=pat: _any(p, [ev.level]))
        elif key == "issue":
            checks.append(lambda ev, p=pat: _any(p, [ev.issue]))
        elif key == "event_id":
            checks.append(lambda ev, p=pat: _any(p, [ev.event_id]))
        elif key == "transaction":
            checks.append(lambda ev, p=pat: _any(p, [ev.transaction]))
    if not checks:
        raise ConfigError(f"{where}: rule has no conditions")
    label = str(obj.get("label") or json.dumps({k: v for k, v in obj.items() if k != "label"}, sort_keys=True))
    return Protection(label, checks)


def load_protections(paths: List[str]) -> List[Protection]:
    out: List[Protection] = []
    for p in paths:
        try:
            obj = json.loads(Path(p).read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ConfigError(f"{p}: no such file") from None
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{p}: invalid JSON ({exc.msg} at line {exc.lineno})") from None
        if isinstance(obj, dict):
            obj = obj.get("must_keep", [obj])
        if not isinstance(obj, list):
            raise ConfigError(f"{p}: expected a list of must-keep rules")
        out.extend(protection_from_obj(o, f"{p}[{i}]") for i, o in enumerate(obj))
    return out
