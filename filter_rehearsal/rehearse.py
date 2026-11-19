"""Run filters over events and build the report."""

from __future__ import annotations

from collections import OrderedDict
from typing import List, Optional

from .events import Event
from .filters import APPROXIMATE, Config
from .protect import Protection


class Verdict:
    __slots__ = ("event", "hits", "protected_by", "baseline_hits")

    def __init__(self, event: Event, hits, protected_by, baseline_hits=None):
        self.event = event
        self.hits = hits  # list of (rule_key, reason)
        self.protected_by = protected_by  # list of labels
        self.baseline_hits = baseline_hits  # None if no baseline

    @property
    def dropped(self) -> bool:
        return bool(self.hits)

    @property
    def new_drop(self) -> bool:
        return self.dropped and self.baseline_hits is not None and not self.baseline_hits


def judge(events: List[Event], config: Config, protections: List[Protection], baseline: Optional[Config] = None) -> List[Verdict]:
    out = []
    for ev in events:
        hits = []
        for rule in config.rules:
            reason = rule.test(ev)
            if reason:
                hits.append((rule.key, reason))
        base = None
        if baseline is not None:
            base = [r.key for r in baseline.rules if r.test(ev)]
        prot = [p.label for p in protections if p.matches(ev)]
        out.append(Verdict(ev, hits, prot, base))
    return out


def summarize(verdicts: List[Verdict], config: Config, baseline: Optional[Config] = None) -> dict:
    total = len(verdicts)
    dropped = [v for v in verdicts if v.dropped]
    by_rule: "OrderedDict[str, dict]" = OrderedDict((r.key, {"events": 0, "issues": set()}) for r in config.rules)
    issues: "OrderedDict[str, dict]" = OrderedDict()
    for v in verdicts:
        key = v.event.group_key()
        it = issues.setdefault(key, {"issue": v.event.issue, "title": v.event.title, "events": 0, "dropped": 0, "rules": []})
        it["events"] += 1
        if v.dropped:
            it["dropped"] += 1
        for rk, _ in v.hits:
            by_rule[rk]["events"] += 1
            by_rule[rk]["issues"].add(key)
            if rk not in it["rules"]:
                it["rules"].append(rk)
    protected_hits = [v for v in verdicts if v.dropped and v.protected_by]
    notes = []
    if config.uses_ip:
        no_ip = sum(1 for v in verdicts if not v.event.ip)
        from_user = sum(1 for v in verdicts if v.event.ip_source == "user.ip_address")
        notes.append(f"{APPROXIMATE['ip']} ({from_user} of {total} events used user.ip_address, {no_ip} had no IP)")
    if config.uses_localhost:
        notes.append(APPROXIMATE["localhost"])
    for label in config.not_rehearsed:
        notes.append(f"not rehearsed: {label} is switched on, but its match list is not documented, so its drops are not counted")
    report = {
        "events": total,
        "issues": len(issues),
        "dropped_events": len(dropped),
        "dropped_issues_fully": sum(1 for i in issues.values() if i["dropped"] == i["events"] and i["events"]),
        "dropped_issues_partly": sum(1 for i in issues.values() if 0 < i["dropped"] < i["events"]),
        "drop_rate": round(len(dropped) / total, 4) if total else 0.0,
        "by_filter": [
            {"filter": k, "events": d["events"], "issues": len(d["issues"])} for k, d in by_rule.items()
        ],
        "issues_list": [
            dict(i, key=k) for k, i in issues.items()
        ],
        "protected_dropped": [
            {
                "event_id": v.event.event_id,
                "title": v.event.title,
                "must_keep": v.protected_by,
                "dropped_by": [rk for rk, _ in v.hits],
                "why": [reason for _, reason in v.hits],
            }
            for v in protected_hits
        ],
        "notes": notes,
    }
    if baseline is not None:
        report["baseline"] = {
            "dropped_events": sum(1 for v in verdicts if v.baseline_hits),
            "newly_dropped": [
                {"event_id": v.event.event_id, "title": v.event.title, "dropped_by": [rk for rk, _ in v.hits]}
                for v in verdicts
                if v.new_drop
            ],
            "no_longer_dropped": [
                {"event_id": v.event.event_id, "title": v.event.title, "was_dropped_by": v.baseline_hits}
                for v in verdicts
                if v.baseline_hits and not v.dropped
            ],
        }
    return report


def _n(k: int, word: str) -> str:
    return f"{k} {word}" + ("" if k == 1 else "s")


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def render_text(report: dict, limit: int = 20) -> str:
    lines = []
    n = report["events"]
    lines.append(f"{n} events in {report['issues']} issues/transactions")
    lines.append(
        f"would drop {report['dropped_events']} events ({_pct(report['drop_rate'])}): "
        f"{report['dropped_issues_fully']} fully, {report['dropped_issues_partly']} partly"
    )
    if report["by_filter"]:
        lines.append("")
        lines.append("by filter")
        rows = [(f["filter"], _n(f["events"], "event"), _n(f["issues"], "issue")) for f in report["by_filter"]]
        w0, w1, w2 = (max(len(r[i]) for r in rows) for i in range(3))
        for a, b, c in rows:
            lines.append(f"  {a:<{w0}}  {b:>{w1}}  {c:>{w2}}")
    issues = [i for i in report["issues_list"] if i["dropped"]]
    if issues:
        lines.append("")
        lines.append("issues hit")
        for i in issues[:limit]:
            tag = "all " if i["dropped"] == i["events"] else "some"
            ident = f"#{i['issue']} " if i["issue"] else ""
            lines.append(f"  {tag} {i['dropped']}/{i['events']}  {ident}{_trim(i['title'])}  <- {', '.join(i['rules'])}")
        if len(issues) > limit:
            lines.append(f"  ... {len(issues) - limit} more (use --format json)")
    if "baseline" in report:
        b = report["baseline"]
        lines.append("")
        lines.append(
            f"vs baseline: baseline drops {b['dropped_events']}, "
            f"{len(b['newly_dropped'])} newly dropped, {len(b['no_longer_dropped'])} no longer dropped"
        )
        for e in b["newly_dropped"][:limit]:
            lines.append(f"  + {e['event_id'][:8]} {_trim(e['title'])}  <- {', '.join(e['dropped_by'])}")
        for e in b["no_longer_dropped"][:limit]:
            lines.append(f"  - {e['event_id'][:8]} {_trim(e['title'])}")
    if report["protected_dropped"]:
        lines.append("")
        lines.append(f"MUST-KEEP EVENTS THAT WOULD BE DROPPED: {len(report['protected_dropped'])}")
        for p in report["protected_dropped"][:limit]:
            lines.append(f"  {p['event_id'][:8]} [{', '.join(p['must_keep'])}] {_trim(p['title'])}")
            for rk, why in zip(p["dropped_by"], p["why"]):
                lines.append(f"      by {rk}: {why}")
    elif n:
        lines.append("")
        lines.append("no must-keep events dropped")
    if report["notes"]:
        lines.append("")
        lines.append("notes")
        for note in report["notes"]:
            lines.append(f"  - {note}")
    return "\n".join(lines) + "\n"


def _trim(text: str, n: int = 60) -> str:
    text = text.replace("\n", " ")
    return text if len(text) <= n else text[: n - 1] + "…"


def render_markdown(report: dict, limit: int = 20) -> str:
    out = ["### filter-rehearsal", ""]
    out.append(
        f"**{report['dropped_events']} of {report['events']} events dropped ({_pct(report['drop_rate'])})**, "
        f"{report['dropped_issues_fully']} issues/transactions fully, {report['dropped_issues_partly']} partly."
    )
    if report["protected_dropped"]:
        out.append("")
        out.append(f"**{len(report['protected_dropped'])} must-keep events would be dropped:**")
        out.append("")
        out.append("| event | must-keep | title | dropped by |")
        out.append("|---|---|---|---|")
        for p in report["protected_dropped"][:limit]:
            out.append(f"| `{p['event_id'][:8]}` | {', '.join(p['must_keep'])} | {_md(p['title'])} | {_md(', '.join(p['dropped_by']))} |")
    else:
        out.append("")
        out.append("No must-keep events dropped.")
    if report["by_filter"]:
        out.append("")
        out.append("| filter | events | issues |")
        out.append("|---|---:|---:|")
        for f in report["by_filter"]:
            out.append(f"| {_md(f['filter'])} | {f['events']} | {f['issues']} |")
    if report["notes"]:
        out.append("")
        out.extend(f"- {n}" for n in report["notes"])
    return "\n".join(out) + "\n"


def _md(text: str) -> str:
    return _trim(text, 80).replace("|", "\\|")


def explain(ev: Event, config: Config) -> str:
    lines = [f"event {ev.event_id or '?'} ({ev.type}) from {ev.source}", f"  title: {ev.title}"]
    lines.append("  error messages list sees: " + (", ".join(repr(s) for s in ev.error_descriptions()) or "(nothing)"))
    lines.append(f"  release: {ev.release!r}  ip: {ev.ip!r} ({ev.ip_source or 'none'})  country: {ev.country!r}")
    if ev.transaction:
        lines.append(f"  transaction: {ev.transaction!r}")
    if ev.url:
        lines.append(f"  url: {ev.url}")
    lines.append("")
    for rule in config.rules:
        reason = rule.test(ev)
        lines.append(f"  {'DROP' if reason else 'pass'}  {rule.key}" + (f"  ({reason})" if reason else ""))
    for label in config.not_rehearsed:
        lines.append(f"  ????  {label} (not rehearsed)")
    return "\n".join(lines) + "\n"
