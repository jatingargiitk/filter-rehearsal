"""Load Sentry events and pull out the fields inbound filters look at.

Two shapes are accepted, auto-detected per event:

* event JSON, as shown by "View JSON" on an event or returned by
  ``/api/0/projects/{org}/{project}/events/{id}/json/``
  (``exception.values``, ``logentry``, ``request.headers`` as pairs,
  ``tags`` as pairs);
* the events API, ``/api/0/projects/{org}/{project}/events/?full=true``
  (``entries[]`` with ``exception`` / ``message`` / ``request`` entries,
  ``tags`` as ``{key, value}`` objects, ``groupID``).

Files may hold one event, a JSON array, JSONL, or a directory of those.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

__all__ = ["Event", "extract", "load_events"]


def _pairs(value: Any) -> Dict[str, str]:
    """headers / tags as dict, from [[k, v]], [{key, value}] or {k: v}."""
    out: Dict[str, str] = {}
    if isinstance(value, dict):
        items = list(value.items())
    elif isinstance(value, list):
        items = []
        for item in value:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                items.append((item[0], item[1]))
            elif isinstance(item, dict) and "key" in item:
                items.append((item["key"], item.get("value")))
    else:
        items = []
    for k, v in items:
        if k is not None and v is not None:
            out[str(k)] = str(v)
    return out


def _entries(raw: dict) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for entry in raw.get("entries") or []:
        if isinstance(entry, dict) and entry.get("type"):
            out.setdefault(entry["type"], entry.get("data"))
    return out


class Event:
    """The subset of a Sentry event that the filters read."""

    def __init__(self, raw: dict, source: str = ""):
        self.raw = raw
        self.source = source
        entries = _entries(raw)

        exc = raw.get("exception")
        if exc is None:
            exc = entries.get("exception")
        values = exc.get("values") if isinstance(exc, dict) else exc
        self.exceptions: List[Tuple[str, str]] = []
        shown: List[Tuple[str, str]] = []
        for item in values or []:
            if not isinstance(item, dict):
                continue
            # inbound filters run before deobfuscation: use the type as received
            typ = item.get("raw_type") or item.get("type") or ""
            val = item.get("value") or ""
            if typ or val:
                self.exceptions.append((str(typ), str(val)))
                shown.append((str(item.get("type") or typ), str(val)))

        msg = None
        for container in (raw.get("logentry"), entries.get("message"), raw.get("message")):
            if isinstance(container, dict):
                msg = container.get("formatted") or container.get("message")
            elif isinstance(container, str):
                msg = container
            if msg:
                break
        self.message: Optional[str] = str(msg) if msg else None

        self.tags = _pairs(raw.get("tags"))
        typ = raw.get("type") or self.tags.get("event.type") or ""
        if not typ:
            typ = "error" if self.exceptions else ("transaction" if raw.get("spans") is not None else "default")
        self.type: str = str(typ).lower()

        rel = raw.get("release")
        if isinstance(rel, dict):
            rel = rel.get("version")
        rel = rel or self.tags.get("release")
        self.release: Optional[str] = str(rel) if rel else None

        user = raw.get("user") if isinstance(raw.get("user"), dict) else {}
        geo = user.get("geo") if isinstance(user.get("geo"), dict) else {}
        country = geo.get("country_code") or self.tags.get("geo.country_code")
        self.country: Optional[str] = str(country).upper() if country else None

        # Sentry filters on the IP the request came from, which an exported
        # event does not record. Use an explicit "_client_ip" if present,
        # else user.ip_address (what "{{auto}}" / sendDefaultPii resolves to).
        ip = raw.get("_client_ip")
        self.ip_source = "_client_ip" if ip else None
        if not ip and user.get("ip_address"):
            ip, self.ip_source = user.get("ip_address"), "user.ip_address"
        self.ip: Optional[str] = str(ip) if ip else None

        request = raw.get("request")
        if not isinstance(request, dict):
            request = entries.get("request") if isinstance(entries.get("request"), dict) else {}
        self.url: Optional[str] = request.get("url") or self.tags.get("url")
        headers = {k.lower(): v for k, v in _pairs(request.get("headers")).items()}
        self.user_agent: Optional[str] = headers.get("user-agent")

        tx = raw.get("transaction") or self.tags.get("transaction")
        self.transaction: Optional[str] = str(tx) if tx else None

        self.level: Optional[str] = (raw.get("level") or self.tags.get("level") or None)
        eid = raw.get("event_id") or raw.get("eventID") or raw.get("id")
        self.event_id: str = str(eid) if eid else ""
        gid = raw.get("groupID") or raw.get("group_id") or raw.get("issue_id")
        self.issue: Optional[str] = str(gid) if gid else None
        title = raw.get("title")
        if not title:
            if shown:
                t, v = shown[-1]
                title = f"{t}: {v}" if t and v else (t or v)
            else:
                title = self.message or self.transaction or "<untitled>"
        self.title: str = str(title)

    # ---- what each filter sees -------------------------------------------
    @property
    def is_transaction(self) -> bool:
        return self.type == "transaction"

    @property
    def is_error(self) -> bool:
        return not self.is_transaction

    def error_descriptions(self) -> List[str]:
        """Strings the Error Messages list is matched against.

        Docs: "{exception.type}: {exception.value}" for error events, the
        formatted message for message events. We check every exception in
        a chain, and the log message too when one is present.
        """
        out = []
        for typ, val in self.exceptions:
            if typ and val:
                out.append(f"{typ}: {val}")
            else:
                out.append(typ or val)
        if self.message:
            out.append(self.message)
        return out

    def error_message_parts(self) -> List[str]:
        """Strings the custom-filter Error Message property checks separately."""
        out = []
        for typ, val in self.exceptions:
            out.extend(x for x in (typ, val) if x)
        if self.message:
            out.append(self.message)
        return out

    def hosts(self) -> List[str]:
        out = []
        if self.url:
            try:
                host = urlparse(self.url).hostname
            except ValueError:
                host = None
            if host:
                out.append(host.lower())
        return out

    def group_key(self) -> str:
        return self.issue or self.title

    def label(self) -> str:
        eid = self.event_id[:8] if self.event_id else "?"
        return f"{eid} {self.title}"


def extract(raw: dict, source: str = "") -> Event:
    return Event(raw, source)


def _coerce(payload: Any, source: str) -> List[dict]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("events", "data", "results"):
            if isinstance(payload.get(key), list):
                return [x for x in payload[key] if isinstance(x, dict)]
        return [payload]
    raise ValueError(f"{source}: expected a JSON object or array of events")


def _load_text(text: str, source: str) -> List[dict]:
    stripped = text.strip()
    if not stripped:
        return []
    try:
        return _coerce(json.loads(stripped), source)
    except json.JSONDecodeError:
        pass
    out: List[dict] = []
    for i, line in enumerate(stripped.splitlines(), 1):
        if not line.strip():
            continue
        try:
            out.extend(_coerce(json.loads(line), f"{source}:{i}"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{source}:{i}: not JSON or JSONL ({exc.msg})") from None
    return out


def load_events(paths: Iterable[str]) -> List[Event]:
    events: List[Event] = []
    for p in paths:
        if p == "-":
            events.extend(Event(r, "stdin") for r in _load_text(sys.stdin.read(), "stdin"))
            continue
        path = Path(p)
        if path.is_dir():
            files = sorted(f for f in path.rglob("*") if f.suffix.lower() in {".json", ".jsonl", ".ndjson"})
            if not files:
                raise ValueError(f"{path}: no .json/.jsonl files")
        elif path.exists():
            files = [path]
        else:
            raise FileNotFoundError(f"{path}: no such file or directory")
        for f in files:
            events.extend(Event(r, str(f)) for r in _load_text(f.read_text(encoding="utf-8"), str(f)))
    return events
