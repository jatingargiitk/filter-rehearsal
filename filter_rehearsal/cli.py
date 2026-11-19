"""filter-rehearsal command line."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List, Optional

from . import __version__
from .events import load_events
from .filters import Config, ConfigError, load_config
from .protect import load_protections
from .rehearse import explain, judge, render_markdown, render_text, summarize

EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 2


def _add_filter_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("-f", "--filters", action="append", default=[], metavar="FILE",
                   help="filter config JSON (repeatable): our format, a project details response, or a data filters list")
    p.add_argument("--try", dest="tries", action="append", default=[], metavar="QUERY",
                   help="custom filter in the UI's key:value syntax, e.g. 'error_type:TypeError release:<2.0.0'")


def _load(args) -> Config:
    cfg = load_config(args.filters, args.tries)
    if not cfg.rules and not cfg.not_rehearsed:
        raise ConfigError("no filters given: pass --filters FILE and/or --try QUERY")
    return cfg


def cmd_run(args) -> int:
    config = _load(args)
    baseline = load_config(args.baseline) if args.baseline else None
    protections = load_protections(args.must_keep)
    events = load_events(args.events)
    if not events:
        raise ConfigError("no events found in the given paths")
    verdicts = judge(events, config, protections, baseline)
    report = summarize(verdicts, config, baseline)
    if args.format == "json":
        sys.stdout.write(json.dumps(report, indent=2, sort_keys=False) + "\n")
    elif args.format == "markdown":
        sys.stdout.write(render_markdown(report, args.limit))
    else:
        sys.stdout.write(render_text(report, args.limit))
    failed = False
    if report["protected_dropped"]:
        failed = True
    if args.max_drop is not None and report["drop_rate"] * 100 > args.max_drop:
        sys.stderr.write(f"drop rate {report['drop_rate'] * 100:.1f}% is above --max-drop {args.max_drop}%\n")
        failed = True
    if args.baseline and args.fail_on_new and report["baseline"]["newly_dropped"]:
        failed = True
    return EXIT_FAIL if failed else EXIT_OK


def cmd_explain(args) -> int:
    config = _load(args)
    events = load_events([args.event])
    if args.event_id:
        events = [e for e in events if e.event_id.startswith(args.event_id)]
        if not events:
            raise ConfigError(f"no event id starting with {args.event_id!r}")
    elif len(events) > 1:
        events = [events[args.index]]
    for ev in events[: 1 if args.event_id else None]:
        sys.stdout.write(explain(ev, config))
    return EXIT_OK


def cmd_fetch(args) -> int:
    from .fetch import FetchError, current_filters, iter_events, write_jsonl

    token = os.environ.get("SENTRY_AUTH_TOKEN")
    if not token:
        raise ConfigError("set SENTRY_AUTH_TOKEN (a token with project:read)")
    try:
        if args.events_out:
            events = list(iter_events(args.base_url, args.org, args.project, token, args.period, args.limit, args.query))
            n = write_jsonl(events, args.events_out)
            sys.stderr.write(f"wrote {n} events to {args.events_out}\n")
        if args.filters_out:
            cfg = current_filters(args.base_url, args.org, args.project, token)
            with open(args.filters_out, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh, indent=2, sort_keys=True)
                fh.write("\n")
            sys.stderr.write(f"wrote current filters to {args.filters_out}\n")
    except FetchError as exc:
        sys.stderr.write(f"filter-rehearsal: {exc}\n")
        return EXIT_FAIL
    if not args.events_out and not args.filters_out:
        raise ConfigError("nothing to do: pass --events-out and/or --filters-out")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="filter-rehearsal",
        description="Rehearse Sentry inbound filters against events you already have, before you switch them on.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="what would these filters drop, and does that include anything you must keep?")
    r.add_argument("events", nargs="+", help="event JSON / JSONL files or directories ('-' for stdin)")
    _add_filter_args(r)
    r.add_argument("-k", "--must-keep", action="append", default=[], metavar="FILE", help="must-keep rules JSON (repeatable)")
    r.add_argument("--baseline", action="append", default=[], metavar="FILE",
                   help="current filter config; report what the new config drops on top of it")
    r.add_argument("--fail-on-new", action="store_true", help="with --baseline: exit 1 if anything is newly dropped")
    r.add_argument("--max-drop", type=float, metavar="PCT", help="exit 1 if more than PCT%% of events would be dropped")
    r.add_argument("--format", choices=["text", "json", "markdown"], default="text")
    r.add_argument("--limit", type=int, default=20, help="rows per section in text/markdown output")
    r.set_defaults(func=cmd_run)

    e = sub.add_parser("explain", help="show what each filter sees in one event and why it passes or drops")
    e.add_argument("event", help="event JSON / JSONL file")
    _add_filter_args(e)
    e.add_argument("--event-id", help="pick the event whose id starts with this")
    e.add_argument("--index", type=int, default=0, help="pick the Nth event in the file (default 0)")
    e.set_defaults(func=cmd_explain)

    f = sub.add_parser("fetch", help="download recent events and/or the current filter settings (read-only)")
    f.add_argument("--org", required=True)
    f.add_argument("--project", required=True)
    f.add_argument("--base-url", default="https://sentry.io", help="self-hosted or regional URL (e.g. https://de.sentry.io)")
    f.add_argument("--events-out", metavar="FILE", help="write events as JSONL")
    f.add_argument("--filters-out", metavar="FILE", help="write the current filter settings as a config file")
    f.add_argument("--period", default="14d", help="statsPeriod, e.g. 24h, 14d (default 14d)")
    f.add_argument("--limit", type=int, default=1000, help="stop after this many events (default 1000)")
    f.add_argument("--query", default="", help="Sentry search query, e.g. 'is:unresolved'")
    f.set_defaults(func=cmd_fetch)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, ValueError, FileNotFoundError, IndexError) as exc:
        sys.stderr.write(f"filter-rehearsal: {exc}\n")
        return EXIT_USAGE


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
