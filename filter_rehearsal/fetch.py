"""Pull recent events and the current filter settings from the Sentry API.

Endpoints (https://docs.sentry.io/api/):
  GET /api/0/projects/{org}/{project}/events/?full=true   events, paginated by Link header
  GET /api/0/projects/{org}/{project}/                    project details ("options" has filters:*)
  GET /api/0/projects/{org}/{project}/filters/            built-in data filters [{id, active}]

Read-only. Token from $SENTRY_AUTH_TOKEN (needs project:read).
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Iterator, List, Optional, Tuple

_LINK = re.compile(r'<([^>]+)>\s*;\s*([^,]*)')


class FetchError(RuntimeError):
    pass


def _next_link(header: Optional[str]) -> Optional[str]:
    if not header:
        return None
    for url, params in _LINK.findall(header):
        attrs = dict(
            (k.strip(), v.strip().strip('"'))
            for k, _, v in (p.partition("=") for p in params.split(";"))
            if k.strip()
        )
        if attrs.get("rel") == "next" and attrs.get("results") == "true":
            return url
    return None


def _get(url: str, token: str, timeout: float = 30.0) -> Tuple[object, Optional[str]]:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            link = resp.headers.get("Link")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        raise FetchError(f"GET {url}: HTTP {exc.code} {detail}") from None
    except urllib.error.URLError as exc:
        raise FetchError(f"GET {url}: {exc.reason}") from None
    try:
        return json.loads(body), link
    except json.JSONDecodeError:
        raise FetchError(f"GET {url}: response is not JSON") from None


def project_url(base: str, org: str, project: str, suffix: str = "") -> str:
    q = urllib.parse.quote
    return f"{base.rstrip('/')}/api/0/projects/{q(org, safe='')}/{q(project, safe='')}/{suffix}"


def iter_events(base: str, org: str, project: str, token: str, period: str = "14d", limit: int = 1000, query: str = "") -> Iterator[dict]:
    params = {"full": "true", "statsPeriod": period}
    if query:
        params["query"] = query
    url: Optional[str] = project_url(base, org, project, "events/") + "?" + urllib.parse.urlencode(params)
    seen = 0
    while url and seen < limit:
        data, link = _get(url, token)
        if not isinstance(data, list):
            raise FetchError(f"GET {url}: expected a list of events")
        for ev in data:
            if seen >= limit:
                return
            seen += 1
            yield ev
        url = _next_link(link)


def current_filters(base: str, org: str, project: str, token: str) -> dict:
    details, _ = _get(project_url(base, org, project), token)
    data_filters, _ = _get(project_url(base, org, project, "filters/"), token)
    if not isinstance(details, dict):
        raise FetchError("project details: expected an object")
    options = details.get("options") or {}
    out = {k: v for k, v in options.items() if k.startswith("filters:")}
    if isinstance(data_filters, list):
        out["data_filters"] = [
            {"id": f.get("id"), "active": f.get("active")}
            for f in data_filters
            if isinstance(f, dict) and f.get("id")
        ]
    return out


def write_jsonl(events: List[dict], path: str) -> int:
    with open(path, "w", encoding="utf-8") as fh:
        for ev in events:
            fh.write(json.dumps(ev, sort_keys=True) + "\n")
    return len(events)
