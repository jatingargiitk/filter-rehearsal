import json

import pytest

from filter_rehearsal.filters import (
    HEALTH_CHECK_GLOBS,
    ConfigError,
    config_from_obj,
    custom_rule,
    load_config,
    parse_custom_query,
)
from helpers import error, transaction


def drops(cfg, ev):
    return [r.key for r in cfg.rules if r.test(ev)]


# ---- built-in lists ------------------------------------------------------
def test_error_messages_match_type_colon_value_case_insensitive():
    cfg = config_from_obj({"error_messages": ["*connectionerror*"]})
    assert drops(cfg, error("ConnectionError", "refused"))
    assert not drops(cfg, error("TimeoutError", "slow"))


def test_type_colon_pattern_only_matches_full_description():
    cfg = config_from_obj({"error_messages": ["TypeError: *"]})
    assert drops(cfg, error("TypeError", "x"))
    assert not drops(cfg, error("RangeError", "TypeError: nested"))


def test_error_messages_never_match_transactions():
    cfg = config_from_obj({"error_messages": ["*"]})
    assert not drops(cfg, transaction("GET /x"))
    assert drops(cfg, error())


def test_error_messages_match_message_events():
    cfg = config_from_obj({"error_messages": ["*disk*full*"]})
    from filter_rehearsal.events import Event

    assert drops(cfg, Event({"type": "default", "logentry": {"formatted": "Disk /var is full"}}))


def test_newline_separated_patterns_like_project_options():
    cfg = config_from_obj({"filters:error_messages": "*a*\n\n  *b*  \n", "filters:releases": ""})
    assert [r.name for r in cfg.rules] == ['"*a*"', '"*b*"']


def test_releases_glob_and_no_release():
    cfg = config_from_obj({"releases": ["my-example@1.*"]})
    assert drops(cfg, error(release="my-example@1.4.0-beta.1"))
    assert not drops(cfg, error(release="my-example@2.0.0"))
    assert not drops(cfg, error(release=None))  # never applies without a release
    assert drops(cfg, transaction("x", release="my-example@1.0"))  # applies to transactions


def test_ip_filters_v4_v6_cidr():
    cfg = config_from_obj({"blacklisted_ips": ["122.33.230.0/30", "1.2.3.4", "a:b:c::0/126", "aaaa:bbbb::cccc"]})
    assert drops(cfg, error(ip="122.33.230.3"))
    assert not drops(cfg, error(ip="122.33.230.4"))
    assert drops(cfg, error(ip="1.2.3.4"))
    assert drops(cfg, error(ip="a:b:c::3"))
    assert drops(cfg, error(ip="aaaa:bbbb::cccc"))
    assert not drops(cfg, error(ip=None))
    assert not drops(cfg, error(ip="not-an-ip"))
    assert cfg.uses_ip


def test_bad_ip_is_a_config_error():
    with pytest.raises(ConfigError, match="not a valid IP"):
        config_from_obj({"blacklisted_ips": ["300.1.1.1"]})


def test_localhost():
    cfg = config_from_obj({"localhost": True})
    assert drops(cfg, error(ip="127.0.0.1"))
    assert drops(cfg, error(ip="127.9.9.9"))
    assert drops(cfg, error(ip="::1"))
    assert drops(cfg, error(url="http://localhost:5173/x"))
    assert drops(cfg, error(url="http://app.localhost/x"))
    assert drops(cfg, error(url="http://[::1]:8000/"))
    assert not drops(cfg, error(ip="203.0.113.1", url="https://example.com"))
    assert not config_from_obj({"localhost": False}).rules


@pytest.mark.parametrize(
    "name",
    ["GET /healthcheck", "/api/health-check/deep", "heartbeat", "GET /health", "/healthy", "/healthz",
     "/_health", "/[_health]", "/live", "/livez", "/ready", "/readyz", "GET /ping", "/up"],
)
def test_health_check_globs_from_docs(name):
    cfg = config_from_obj({"health_check": True})
    assert drops(cfg, transaction(name))


def test_health_check_only_transactions_and_not_lookalikes():
    cfg = config_from_obj({"health_check": True})
    assert not drops(cfg, transaction("GET /health/details"))
    assert not drops(cfg, transaction("GET /users/update"))
    assert not drops(cfg, error("E", "GET /ping"))
    assert len(HEALTH_CHECK_GLOBS) == 14


def test_unrehearsable_builtins_are_reported_not_guessed():
    cfg = config_from_obj({"web-crawlers": True, "browser-extensions": False, "legacy-browsers": ["ie11"]})
    assert cfg.rules == []
    assert cfg.not_rehearsed == ["web crawlers", "legacy browser versions"]


def test_data_filters_list_shape():
    cfg = config_from_obj(
        [
            {"id": "localhost", "active": True},
            {"id": "filtered-transaction", "active": False},
            {"id": "legacy-browsers", "active": []},
            {"id": "something-new", "active": True},
        ]
    )
    assert [r.kind for r in cfg.rules] == ["localhost"]
    assert cfg.not_rehearsed == ["built-in filter 'something-new'"]


def test_project_details_shape():
    cfg = config_from_obj({"slug": "web", "options": {"filters:releases": "a@1.*", "filters:error_messages": "", "sentry:x": 1}})
    assert [r.key for r in cfg.rules] == ['releases "a@1.*"']


def test_unknown_key_and_bad_glob():
    with pytest.raises(ConfigError, match="unknown key 'error_message'"):
        config_from_obj({"error_message": ["x"]})
    with pytest.raises(ConfigError, match="unclosed"):
        config_from_obj({"releases": ["[abc"]})


# ---- custom filters ------------------------------------------------------
def rule(conditions, data_type="error", **kw):
    return custom_rule({"name": "t", "dataType": data_type, "conditions": conditions, **kw})


def test_docs_example_custom_filter():
    r = rule(
        [
            {"type": "error_type", "operator": "equals", "value": ["TypeError"]},
            {"type": "error_message", "operator": "contains", "value": ["undefined"]},
            {"type": "release", "operator": "lt", "value": ["2.0.0"]},
        ]
    )
    assert r.test(error("TypeError", "x is undefined", release="web@1.9.0"))
    assert not r.test(error("TypeError", "x is undefined", release="web@2.0.0"))
    assert not r.test(error("RangeError", "undefined", release="web@1.0.0"))
    assert not r.test(error("TypeError", "null", release="web@1.0.0"))


def test_equals_is_exact_unless_star():
    r = rule([{"type": "error_type", "operator": "equals", "value": ["TypeError"]}])
    assert r.test(error("typeerror", "x"))  # not case-sensitive
    assert not r.test(error("TypeErrorX", "x"))
    r2 = rule([{"type": "error_message", "operator": "equals", "value": ["*ConnectionError*"]}])
    assert r2.test(error("requests.ConnectionError", "x"))


def test_error_message_checks_parts_separately():
    # type, value and message are checked one by one, not as "type: value"
    r = rule([{"type": "error_message", "operator": "equals", "value": ["TypeError: *"]}])
    assert not r.test(error("TypeError", "x"))
    r2 = rule([{"type": "error_message", "operator": "starts_with", "value": ["Type"]}])
    assert r2.test(error("TypeError", "x"))


def test_error_type_any_in_chain():
    from filter_rehearsal.events import Event

    ev = Event({"exception": {"values": [{"type": "KeyError", "value": "a"}, {"type": "RuntimeError", "value": "b"}]}})
    assert rule([{"type": "error_type", "operator": "equals", "value": ["KeyError"]}]).test(ev)


def test_list_of_values_is_or():
    r = rule([{"type": "geo_country_code", "operator": "equals", "value": ["CN", "RU"]}])
    assert r.test(error(user={"geo": {"country_code": "ru"}}))
    assert not r.test(error(user={"geo": {"country_code": "US"}}))


def test_negated_operators_match_missing_values():
    r = rule([{"type": "release", "operator": "does_not_contain", "value": ["beta"]}])
    assert r.test(error(release="web@1.0.0"))
    assert not r.test(error(release="web@1.0.0-beta.2"))
    assert r.test(error(release=None))  # docs: also drops data sent without a release
    r2 = rule([{"type": "release", "operator": "does_not_start_with", "value": ["web@"]}])
    assert r2.test(error(release="api@1")) and not r2.test(error(release="web@1"))
    r3 = rule([{"type": "release", "operator": "does_not_end_with", "value": ["-rc"]}])
    assert not r3.test(error(release="web@1-rc"))


def test_release_range_is_two_conditions():
    r = rule(
        [
            {"type": "release", "operator": "gte", "value": ["2.0.0"]},
            {"type": "release", "operator": "lt", "value": ["4.0.0"]},
        ]
    )
    assert r.test(error(release="web@3.1.0"))
    assert not r.test(error(release="web@4.0.0"))
    assert not r.test(error(release="web@1.9.0"))
    assert not r.test(error(release="0f3c2a1"))  # commit hash never compares


def test_comparison_errors():
    with pytest.raises(ConfigError, match="has no version"):
        rule([{"type": "release", "operator": "lt", "value": ["deadbeef"]}])
    with pytest.raises(ConfigError, match="exactly one value"):
        rule([{"type": "release", "operator": "lt", "value": ["1", "2"]}])
    with pytest.raises(ConfigError, match="only works on release"):
        rule([{"type": "error_type", "operator": "gt", "value": ["1"]}])


def test_ip_cidr_and_not_cidr():
    r = rule([{"type": "ip_address", "operator": "cidr", "value": ["10.0.0.0/8", "192.168.0.0/16"]}])
    assert r.test(error(ip="10.1.2.3")) and not r.test(error(ip="8.8.8.8"))
    r2 = rule([{"type": "ip_address", "operator": "not_cidr", "value": ["10.0.0.0/8"]}])
    assert r2.test(error(ip="8.8.8.8")) and not r2.test(error(ip="10.0.0.1"))
    assert r2.test(error(ip=None))  # negated also matches data with no value
    with pytest.raises(ConfigError, match="cidr/not_cidr"):
        rule([{"type": "ip_address", "operator": "equals", "value": ["10.0.0.1"]}])


def test_ip_only_filter_drops_every_type():
    r = rule([{"type": "ip_address", "operator": "cidr", "value": ["10.0.0.0/8"]}], data_type="error")
    assert r.test(transaction("GET /x", ip="10.0.0.1"))
    r2 = rule(
        [{"type": "ip_address", "operator": "cidr", "value": ["10.0.0.0/8"]}, {"type": "release", "operator": "contains", "value": ["api"]}],
        data_type="error",
    )
    assert not r2.test(transaction("GET /x", ip="10.0.0.1"))


def test_data_types():
    cond = [{"type": "release", "operator": "contains", "value": ["web"]}]
    assert rule(cond, "error").test(error(release="web@1"))
    assert not rule(cond, "error").test(transaction("t", release="web@1"))
    assert rule(cond, "all").test(transaction("t", release="web@1"))
    assert rule(cond, "span").test(transaction("t", release="web@1"))
    assert not rule(cond, "log").test(error(release="web@1"))
    err_only = [{"type": "error_type", "operator": "equals", "value": ["*"]}]
    assert not rule(err_only, "all").test(transaction("t"))


def test_inactive_and_limits():
    assert custom_rule({"name": "off", "active": False, "conditions": [{"type": "release", "value": ["x"]}]}) is None
    many = [{"type": "release", "operator": "contains", "value": ["x"]}] * 11
    with pytest.raises(ConfigError, match="at most 10"):
        rule(many)
    with pytest.raises(ConfigError, match="logs/metrics"):
        rule([{"type": "log_message", "value": ["x"]}])
    with pytest.raises(ConfigError, match="unknown operator"):
        rule([{"type": "release", "operator": "regex", "value": ["x"]}])
    with pytest.raises(ConfigError, match="non-empty list"):
        rule([{"type": "release", "value": []}])


def test_parse_custom_query():
    q = parse_custom_query("error_type:TypeError error_message:*undefined* release:<2.0.0")
    assert q["conditions"] == [
        {"type": "error_type", "operator": "equals", "value": ["TypeError"]},
        {"type": "error_message", "operator": "equals", "value": ["*undefined*"]},
        {"type": "release", "operator": "lt", "value": ["2.0.0"]},
    ]
    q2 = parse_custom_query("geo_country_code:[CN,RU] ip_address:[10.0.0.0/8,192.168.0.0/16] release:>=1.2")
    assert q2["conditions"][0]["value"] == ["CN", "RU"]
    assert q2["conditions"][1] == {"type": "ip_address", "operator": "cidr", "value": ["10.0.0.0/8", "192.168.0.0/16"]}
    assert q2["conditions"][2]["operator"] == "gte"
    q3 = parse_custom_query('error_message:"*connection reset*"')
    assert q3["conditions"][0]["value"] == ["*connection reset*"]
    with pytest.raises(ConfigError):
        parse_custom_query("justaword")


def test_load_config_files_and_tries(tmp_path):
    p = tmp_path / "f.json"
    p.write_text(json.dumps({"releases": ["a@*"], "custom": [{"name": "c", "conditions": [{"type": "release", "value": ["b@1"]}]}]}))
    cfg = load_config([str(p)], ["ip_address:10.0.0.0/8"])
    assert [r.kind for r in cfg.rules] == ["releases", "custom", "custom"]
    assert cfg.uses_ip
    bad = tmp_path / "bad.json"
    bad.write_text("{")
    with pytest.raises(ConfigError, match="invalid JSON"):
        load_config([str(bad)])
