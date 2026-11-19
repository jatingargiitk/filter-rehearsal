"""Inbound filter rules, re-implemented from the public Sentry docs.

Source of truth: https://docs.sentry.io/concepts/data-management/filtering/
No Sentry/Relay code is used. Where the docs leave behaviour open we pick
one reading and say so (see ``APPROXIMATE``).
"""

from __future__ import annotations

import ipaddress
import json
import shlex
from pathlib import Path
from typing import Any, Callable, Iterable, List, Optional, Tuple

from .events import Event
from .globs import GlobError, compile_glob, glob_match
from .ips import IPError, ip_in, parse_network
from .versions import compare as version_compare
from .versions import parse_release

__all__ = [
    "ConfigError",
    "Rule",
    "Config",
    "load_config",
    "parse_custom_query",
    "HEALTH_CHECK_GLOBS",
]

# docs: "We consider a transaction to be a health check if its name matches
# one of the following glob patterns"
HEALTH_CHECK_GLOBS = [
    "*healthcheck*",
    "*health-check*",
    "*heartbeat*",
    "*/health",
    "*/healthy",
    "*/healthz",
    "*/_health",
    # the docs list "*/[_health]"; we read it as the literal path segment
    r"*/\[_health\]",
    "*/live",
    "*/livez",
    "*/ready",
    "*/readyz",
    "*/ping",
    "*/up",
]

# built-in filters we know about but cannot rehearse honestly: the docs do
# not publish their match lists (they live in Relay, which is FSL-licensed,
# and we do not copy from it).
NOT_REHEARSED = {
    "browser-extensions": "browser extension errors",
    "web-crawlers": "web crawlers",
    "legacy-browsers": "legacy browser versions",
    "filters:react-hydration-errors": "React hydration errors",
    "filters:chunk-load-error": "ChunkLoadErrors",
}

APPROXIMATE = {
    "localhost": "localhost: matches loopback IPs (127.0.0.0/8, ::1) or a request URL host of localhost/*.localhost/127.x/::1",
    "ip": "IP filters use the IP the request came from; exports do not keep it, so user.ip_address (or _client_ip) is used",
}


class ConfigError(ValueError):
    pass


class Rule:
    """One thing that can drop an event. ``test`` returns a reason or None."""

    def __init__(self, kind: str, name: str, test: Callable[[Event], Optional[str]]):
        self.kind = kind
        self.name = name
        self.test = test

    @property
    def key(self) -> str:
        return f"{self.kind} {self.name}" if self.name else self.kind

    def __repr__(self) -> str:  # pragma: no cover
        return f"Rule({self.key!r})"


class Config:
    def __init__(self) -> None:
        self.rules: List[Rule] = []
        self.not_rehearsed: List[str] = []
        self.uses_ip = False
        self.uses_localhost = False
        self.sources: List[str] = []

    def extend(self, other: "Config") -> None:
        self.rules.extend(other.rules)
        for item in other.not_rehearsed:
            if item not in self.not_rehearsed:
                self.not_rehearsed.append(item)
        self.uses_ip = self.uses_ip or other.uses_ip
        self.uses_localhost = self.uses_localhost or other.uses_localhost
        self.sources.extend(other.sources)


# ---------------------------------------------------------------- helpers
def _lines(value: Any, what: str) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = value.splitlines()
    elif isinstance(value, list):
        items = []
        for x in value:
            if not isinstance(x, str):
                raise ConfigError(f"{what}: expected strings, got {x!r}")
            items.append(x)
    else:
        raise ConfigError(f"{what}: expected a list of patterns or a newline-separated string")
    return [x.strip() for x in items if x.strip()]


def _check_globs(patterns: List[str], what: str) -> None:
    for p in patterns:
        try:
            compile_glob(p)
        except GlobError as exc:
            raise ConfigError(f"{what}: {exc}") from None


def _short(text: str, n: int = 70) -> str:
    text = text.replace("\n", " ")
    return text if len(text) <= n else text[: n - 1] + "…"


# ------------------------------------------------------------ built-ins
def error_messages_rule(pattern: str) -> Rule:
    def test(ev: Event) -> Optional[str]:
        if ev.is_transaction:  # "Transactions are never matched by this filter."
            return None
        for s in ev.error_descriptions():
            if glob_match(pattern, s):
                return f"matched {_short(s)!r}"
        return None

    return Rule("error_messages", json.dumps(pattern), test)


def releases_rule(pattern: str) -> Rule:
    def test(ev: Event) -> Optional[str]:
        # "The filter never applies to events without a release."
        if ev.release and glob_match(pattern, ev.release):
            return f"release {ev.release!r}"
        return None

    return Rule("releases", json.dumps(pattern), test)


def ips_rule(value: str) -> Rule:
    try:
        net = parse_network(value)
    except IPError as exc:
        raise ConfigError(f"blacklisted_ips: {exc}") from None

    def test(ev: Event) -> Optional[str]:
        if ip_in(ev.ip, [net]):
            return f"ip {ev.ip} ({ev.ip_source})"
        return None

    return Rule("blacklisted_ips", value, test)


_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}


def _is_loopback(text: Optional[str]) -> bool:
    if not text:
        return False
    try:
        return ipaddress.ip_address(text.strip("[]")).is_loopback
    except ValueError:
        return False


def localhost_rule() -> Rule:
    def test(ev: Event) -> Optional[str]:
        if _is_loopback(ev.ip):
            return f"ip {ev.ip} is loopback"
        for host in ev.hosts():
            if host in _LOOPBACK_HOSTS or host.endswith(".localhost") or _is_loopback(host):
                return f"url host {host}"
        return None

    return Rule("localhost", "", test)


def health_check_rule() -> Rule:
    def test(ev: Event) -> Optional[str]:
        if not ev.is_transaction or not ev.transaction:
            return None
        for pat in HEALTH_CHECK_GLOBS:
            if glob_match(pat, ev.transaction):
                return f"transaction {ev.transaction!r} ~ {pat}"
        return None

    return Rule("health_check", "", test)


# ---------------------------------------------------------- custom filters
TEXT_PROPS = {"error_message", "error_type", "geo_country_code", "release"}
ERROR_ONLY_PROPS = {"error_message", "error_type", "geo_country_code"}
UNSUPPORTED_PROPS = {"log_message", "metric_name"}
TEXT_OPS = {"equals", "contains", "starts_with", "ends_with"}
NEG_OPS = {"does_not_contain": "contains", "does_not_start_with": "starts_with", "does_not_end_with": "ends_with"}
VERSION_OPS = {"gt", "gte", "lt", "lte"}
IP_OPS = {"cidr", "not_cidr"}


def _prop_values(ev: Event, prop: str) -> List[str]:
    if prop == "error_message":
        return ev.error_message_parts()
    if prop == "error_type":
        return [t for t, _ in ev.exceptions if t]
    if prop == "geo_country_code":
        return [ev.country] if ev.country else []
    if prop == "release":
        return [ev.release] if ev.release else []
    if prop == "ip_address":
        return [ev.ip] if ev.ip else []
    raise ConfigError(f"unknown condition type {prop!r}")


def _text_match(op: str, have: str, want: str) -> bool:
    h, w = have.lower(), want.lower()
    if op == "equals":
        return glob_match(want, have) if "*" in want else h == w
    if op == "contains":
        return w in h
    if op == "starts_with":
        return h.startswith(w)
    if op == "ends_with":
        return h.endswith(w)
    raise AssertionError(op)


def _condition(cond: dict, where: str) -> Tuple[str, Callable[[Event], bool], str]:
    if not isinstance(cond, dict):
        raise ConfigError(f"{where}: condition must be an object")
    prop = cond.get("type")
    op = cond.get("operator", "equals")
    values = cond.get("value")
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list) or not values or not all(isinstance(v, str) for v in values):
        raise ConfigError(f"{where}: 'value' must be a non-empty list of strings")
    if prop in UNSUPPORTED_PROPS:
        raise ConfigError(f"{where}: {prop} applies to logs/metrics, which this tool does not rehearse")
    if prop not in TEXT_PROPS and prop != "ip_address":
        raise ConfigError(f"{where}: unknown condition type {prop!r}")
    desc = f"{prop}:{op}:{','.join(values)}"

    if prop == "ip_address":
        if op not in IP_OPS:
            raise ConfigError(f"{where}: ip_address takes cidr/not_cidr, not {op!r}")
        try:
            nets = [parse_network(v) for v in values]
        except IPError as exc:
            raise ConfigError(f"{where}: {exc}") from None
        if op == "cidr":
            return prop, (lambda ev: ip_in(ev.ip, nets)), desc
        # "A negated operator also matches data that has no value"
        return prop, (lambda ev: not ip_in(ev.ip, nets)), desc

    if op in VERSION_OPS:
        if prop != "release":
            raise ConfigError(f"{where}: {op} only works on release")
        if len(values) != 1:
            raise ConfigError(f"{where}: {op} takes exactly one value")
        if parse_release(values[0]) is None:
            raise ConfigError(f"{where}: {values[0]!r} has no version (Sentry rejects it)")
        val = values[0]
        return prop, (lambda ev: version_compare(op, ev.release, val)), desc

    if op in TEXT_OPS:
        for v in values:
            if op == "equals" and "*" in v:
                try:
                    compile_glob(v)
                except GlobError as exc:
                    raise ConfigError(f"{where}: {exc}") from None

        def positive(ev: Event, _op=op) -> bool:
            return any(_text_match(_op, h, w) for h in _prop_values(ev, prop) for w in values)

        return prop, positive, desc

    if op in NEG_OPS:
        base = NEG_OPS[op]

        def negative(ev: Event) -> bool:
            # "No value matches"; data without the property also matches
            return not any(_text_match(base, h, w) for h in _prop_values(ev, prop) for w in values)

        return prop, negative, desc

    raise ConfigError(f"{where}: unknown operator {op!r}")


DATA_TYPES = {"error", "errors", "all", "span", "spans", "log", "logs", "metric", "metrics", "trace_metric"}


def custom_rule(spec: dict, where: str = "custom") -> Optional[Rule]:
    if not isinstance(spec, dict):
        raise ConfigError(f"{where}: custom filter must be an object")
    name = spec.get("name") or "<unnamed>"
    where = f"{where} {name!r}"
    if spec.get("active", True) is False:
        return None
    dtype = str(spec.get("dataType", "error")).lower()
    if dtype not in DATA_TYPES:
        raise ConfigError(f"{where}: unknown dataType {dtype!r}")
    conds = spec.get("conditions")
    if not isinstance(conds, list) or not conds:
        raise ConfigError(f"{where}: needs at least one condition")
    if len(conds) > 10:
        raise ConfigError(f"{where}: at most 10 conditions (Sentry limit)")
    parsed = [_condition(c, where) for c in conds]
    props = {p for p, _, _ in parsed}
    ip_only = props == {"ip_address"}

    def applies(ev: Event) -> bool:
        if ip_only:  # "drops matching data of every type, whatever its data type"
            return True
        if dtype in {"error", "errors"}:
            return ev.is_error
        if dtype in {"span", "spans"}:
            return ev.is_transaction
        if dtype == "all":
            return ev.is_error or not (props & ERROR_ONLY_PROPS)
        return False  # logs / metrics: not events

    def test(ev: Event) -> Optional[str]:
        if not applies(ev):
            return None
        if all(fn(ev) for _, fn, _ in parsed):  # conditions are ANDed
            return " & ".join(d for _, _, d in parsed)
        return None

    return Rule("custom", json.dumps(name), test)


_SHORT_OPS = [(">=", "gte"), ("<=", "lte"), (">", "gt"), ("<", "lt")]


def parse_custom_query(query: str, name: Optional[str] = None, data_type: str = "error") -> dict:
    """``error_type:TypeError error_message:*undefined* release:<2.0.0``.

    The UI's ``key:value`` syntax: ``key:[a,b]`` for several values,
    ``release:>=1.2`` for version comparisons, ``ip_address:10.0.0.0/8``.
    A value with ``*`` is a glob; otherwise ``key:value`` is an exact,
    case-insensitive match.
    """
    try:
        tokens = shlex.split(query)
    except ValueError as exc:
        raise ConfigError(f"--try: {exc}") from None
    if not tokens:
        raise ConfigError("--try: empty query")
    conditions = []
    for tok in tokens:
        if ":" not in tok:
            raise ConfigError(f"--try: expected key:value, got {tok!r}")
        key, val = tok.split(":", 1)
        op = "equals"
        if key == "ip_address":
            op = "cidr"
        elif key == "release":
            for prefix, name_ in _SHORT_OPS:
                if val.startswith(prefix):
                    op, val = name_, val[len(prefix):]
                    break
        if val.startswith("[") and val.endswith("]"):
            values = [v.strip() for v in val[1:-1].split(",") if v.strip()]
        else:
            values = [val]
        conditions.append({"type": key, "operator": op, "value": values})
    return {"name": name or query, "dataType": data_type, "conditions": conditions}


# --------------------------------------------------------------- loading
_BUILTIN_IDS = {
    "localhost": "localhost",
    "filtered-transaction": "health_check",
    "health_check": "health_check",
    "health-check": "health_check",
    "browser-extensions": "browser-extensions",
    "browser_extensions": "browser-extensions",
    "web-crawlers": "web-crawlers",
    "web_crawlers": "web-crawlers",
    "legacy-browsers": "legacy-browsers",
    "legacy_browsers": "legacy-browsers",
    "react_hydration_errors": "filters:react-hydration-errors",
    "filters:react-hydration-errors": "filters:react-hydration-errors",
    "chunk_load_errors": "filters:chunk-load-error",
    "filters:chunk-load-error": "filters:chunk-load-error",
}

_LIST_KEYS = {
    "error_messages": "error_messages",
    "filters:error_messages": "error_messages",
    "releases": "releases",
    "filters:releases": "releases",
    "blacklisted_ips": "blacklisted_ips",
    "ips": "blacklisted_ips",
    "filters:blacklisted_ips": "blacklisted_ips",
}

_IGNORED_KEYS = {"$schema", "comment", "_comment", "filters:log_messages", "filters:trace_metric_names", "log_messages", "metric_names"}


def _toggle(cfg: Config, builtin: str, on: Any, where: str) -> None:
    active = bool(on) and on not in ("0", "false")
    if isinstance(on, list):  # legacy-browsers: list of subfilters
        active = len(on) > 0
    if not active:
        return
    if builtin == "localhost":
        cfg.rules.append(localhost_rule())
        cfg.uses_localhost = True
    elif builtin == "health_check":
        cfg.rules.append(health_check_rule())
    else:
        label = NOT_REHEARSED[builtin]
        if label not in cfg.not_rehearsed:
            cfg.not_rehearsed.append(label)


def config_from_obj(obj: Any, where: str = "config") -> Config:
    cfg = Config()
    cfg.sources.append(where)
    # data filters endpoint: [{"id": "localhost", "active": true}, ...]
    if isinstance(obj, list):
        for item in obj:
            if not isinstance(item, dict) or "id" not in item:
                raise ConfigError(f"{where}: expected a list of {{id, active}} data filters")
            builtin = _BUILTIN_IDS.get(item["id"])
            if builtin is None:
                active = item.get("active")
                if active and active not in ("0", "false"):
                    label = f"built-in filter {item['id']!r}"
                    if label not in cfg.not_rehearsed:
                        cfg.not_rehearsed.append(label)
                continue
            _toggle(cfg, builtin, item.get("active"), where)
        return cfg
    if not isinstance(obj, dict):
        raise ConfigError(f"{where}: expected a JSON object")
    # project details endpoint: filters live in "options"
    if isinstance(obj.get("options"), dict) and ("slug" in obj or "filters:releases" in obj["options"] or any(k.startswith("filters:") for k in obj["options"])):
        sub = {k: v for k, v in obj["options"].items() if k.startswith("filters:")}
        return config_from_obj(sub, where)
    for key, value in obj.items():
        if key in _IGNORED_KEYS:
            continue
        if key in _LIST_KEYS:
            kind = _LIST_KEYS[key]
            items = _lines(value, f"{where}: {key}")
            if kind == "blacklisted_ips":
                cfg.rules.extend(ips_rule(v) for v in items)
                cfg.uses_ip = cfg.uses_ip or bool(items)
            else:
                _check_globs(items, f"{where}: {key}")
                make = error_messages_rule if kind == "error_messages" else releases_rule
                cfg.rules.extend(make(p) for p in items)
        elif key in _BUILTIN_IDS:
            _toggle(cfg, _BUILTIN_IDS[key], value, where)
        elif key in ("custom", "custom_filters", "customFilters"):
            if not isinstance(value, list):
                raise ConfigError(f"{where}: {key} must be a list")
            for i, spec in enumerate(value):
                rule = custom_rule(spec, f"{where}: {key}[{i}]")
                if rule is not None:
                    cfg.rules.append(rule)
                    if any(isinstance(c, dict) and c.get("type") == "ip_address" for c in spec.get("conditions", [])):
                        cfg.uses_ip = True
        elif key == "data_filters":
            cfg.extend(config_from_obj(value, f"{where}: data_filters"))
        else:
            raise ConfigError(f"{where}: unknown key {key!r}")
    return cfg


def load_config(paths: Iterable[str], tries: Iterable[str] = ()) -> Config:
    cfg = Config()
    for p in paths:
        path = Path(p)
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ConfigError(f"{p}: no such file") from None
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{p}: invalid JSON ({exc.msg} at line {exc.lineno})") from None
        cfg.extend(config_from_obj(obj, str(p)))
    for q in tries:
        rule = custom_rule(parse_custom_query(q), "--try")
        if rule is not None:
            cfg.rules.append(rule)
            if "ip_address:" in q:
                cfg.uses_ip = True
        cfg.sources.append(f"--try {q}")
    return cfg
