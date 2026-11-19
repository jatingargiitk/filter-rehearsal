import json

from filter_rehearsal.events import Event, load_events

API_SHAPE = {
    "eventID": "abc123",
    "groupID": "777",
    "title": "TypeError: x is undefined",
    "tags": [{"key": "release", "value": "web@2.0.0"}, {"key": "level", "value": "error"}, {"key": "event.type", "value": "error"}],
    "user": {"ip_address": "203.0.113.5", "geo": {"country_code": "de"}},
    "entries": [
        {"type": "exception", "data": {"values": [{"type": "TypeError", "value": "x is undefined"}]}},
        {"type": "request", "data": {"url": "http://localhost:3000/a", "headers": [["User-Agent", "UA/1"]]}},
    ],
}


def test_api_shape():
    ev = Event(API_SHAPE)
    assert ev.event_id == "abc123" and ev.issue == "777"
    assert ev.release == "web@2.0.0"
    assert ev.error_descriptions() == ["TypeError: x is undefined"]
    assert ev.country == "DE" and ev.ip == "203.0.113.5" and ev.ip_source == "user.ip_address"
    assert ev.hosts() == ["localhost"] and ev.user_agent == "UA/1"
    assert ev.level == "error" and ev.is_error


def test_raw_type_is_what_filters_see_but_title_is_deobfuscated():
    ev = Event({"exception": {"values": [{"type": "com.x.CartException", "raw_type": "a.b", "value": "empty"}]}})
    assert ev.error_descriptions() == ["a.b: empty"]
    assert ev.title == "com.x.CartException: empty"


def test_chained_exceptions_and_message():
    ev = Event(
        {
            "exception": {"values": [{"type": "KeyError", "value": "'id'"}, {"type": "RuntimeError", "value": "wrapped"}]},
            "logentry": {"formatted": "failed to load order 12"},
        }
    )
    assert ev.error_descriptions() == ["KeyError: 'id'", "RuntimeError: wrapped", "failed to load order 12"]
    assert ev.error_message_parts() == ["KeyError", "'id'", "RuntimeError", "wrapped", "failed to load order 12"]


def test_message_event_and_transaction_type():
    msg = Event({"type": "default", "logentry": {"message": "disk %s full", "formatted": "disk /var full"}})
    assert msg.is_error and msg.error_descriptions() == ["disk /var full"]
    tx = Event({"type": "transaction", "transaction": "GET /healthz"})
    assert tx.is_transaction and tx.title == "GET /healthz"


def test_type_inferred_when_missing():
    assert Event({"spans": [], "transaction": "x"}).is_transaction
    assert Event({"exception": {"values": [{"type": "E", "value": "v"}]}}).type == "error"


def test_client_ip_overrides_user_ip():
    ev = Event({"_client_ip": "10.0.0.1", "user": {"ip_address": "1.2.3.4"}})
    assert ev.ip == "10.0.0.1" and ev.ip_source == "_client_ip"


def test_loaders(tmp_path):
    a = {"event_id": "a", "message": "one"}
    b = {"event_id": "b", "message": "two"}
    (tmp_path / "one.json").write_text(json.dumps(a))
    (tmp_path / "arr.json").write_text(json.dumps([a, b]))
    (tmp_path / "lines.jsonl").write_text(json.dumps(a) + "\n\n" + json.dumps(b) + "\n")
    (tmp_path / "wrapped.json").write_text(json.dumps({"events": [a]}))
    (tmp_path / "ignored.txt").write_text("nope")
    assert [e.event_id for e in load_events([str(tmp_path / "one.json")])] == ["a"]
    assert [e.event_id for e in load_events([str(tmp_path / "lines.jsonl")])] == ["a", "b"]
    assert len(load_events([str(tmp_path)])) == 6


def test_loader_errors(tmp_path):
    import pytest

    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"a": 1}\n{oops\n')
    with pytest.raises(ValueError, match="bad.jsonl:2"):
        load_events([str(bad)])
    with pytest.raises(FileNotFoundError):
        load_events([str(tmp_path / "missing.json")])
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError, match="no .json"):
        load_events([str(empty)])
