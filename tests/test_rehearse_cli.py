import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from filter_rehearsal.cli import main
from filter_rehearsal.filters import ConfigError, config_from_obj
from filter_rehearsal.protect import load_protections, protection_from_obj
from filter_rehearsal.rehearse import judge, summarize
from helpers import error, transaction

EX = Path(__file__).resolve().parent.parent / "examples"


def test_protection_rules():
    p = protection_from_obj({"label": "pay", "error_type": "Payment*", "tag": {"flow": "check*"}}, "t")
    assert p.matches(error("PaymentDeclined", "x", tags={"flow": "checkout"}))
    assert not p.matches(error("PaymentDeclined", "x"))  # AND within one rule
    assert protection_from_obj({"level": "fatal"}, "t").matches(error(level="FATAL"))
    assert protection_from_obj({"issue": "42"}, "t").matches(error(groupID="42"))
    assert protection_from_obj({"message": "*refund*"}, "t").matches(error("E", "Refund failed"))
    assert protection_from_obj({"transaction": "*/checkout*"}, "t").matches(transaction("POST /checkout/pay"))
    assert protection_from_obj({"release": "web@1.*"}, "t").label == '{"release": "web@1.*"}'
    with pytest.raises(ConfigError, match="unknown key"):
        protection_from_obj({"titel": "x"}, "t")
    with pytest.raises(ConfigError, match="no conditions"):
        protection_from_obj({"label": "x"}, "t")


def test_load_protections_shapes(tmp_path):
    a = tmp_path / "a.json"
    a.write_text(json.dumps({"must_keep": [{"level": "fatal"}]}))
    b = tmp_path / "b.json"
    b.write_text(json.dumps({"label": "one", "issue": "1"}))
    assert [p.label for p in load_protections([str(a), str(b)])] == ['{"level": "fatal"}', "one"]


def test_summary_counts_and_baseline():
    events = [
        error("E", "noise", groupID="1", event_id="a" * 32),
        error("E", "noise", groupID="1", event_id="b" * 32, release="web@9.0.0"),
        error("PaymentError", "declined", groupID="2", event_id="c" * 32),
        error("E", "fine", groupID="3", event_id="d" * 32),
    ]
    cfg = config_from_obj({"error_messages": ["*noise*", "*declined*"]})
    base = config_from_obj({"releases": ["web@9.*"]})
    prot = [protection_from_obj({"label": "payments", "error_type": "Payment*"}, "t")]
    rep = summarize(judge(events, cfg, prot, base), cfg, base)
    assert rep["events"] == 4 and rep["issues"] == 3 and rep["dropped_events"] == 3
    assert rep["dropped_issues_fully"] == 2 and rep["dropped_issues_partly"] == 0
    assert rep["drop_rate"] == 0.75
    assert [f["events"] for f in rep["by_filter"]] == [2, 1]
    assert rep["protected_dropped"][0]["must_keep"] == ["payments"]
    assert [e["event_id"] for e in rep["baseline"]["newly_dropped"]] == ["a" * 32, "c" * 32]
    assert rep["baseline"]["dropped_events"] == 1 and rep["baseline"]["no_longer_dropped"] == []


def test_partly_dropped_issue():
    events = [error(groupID="9", release="web@1.0"), error(groupID="9", release="web@2.0")]
    cfg = config_from_obj({"releases": ["web@1.*"]})
    rep = summarize(judge(events, cfg, []), cfg)
    assert rep["dropped_issues_partly"] == 1 and rep["dropped_issues_fully"] == 0


def run(args, capsys):
    code = main(args)
    out = capsys.readouterr()
    return code, out.out, out.err


def test_cli_example_fails_on_must_keep(capsys):
    code, out, _ = run(["run", str(EX / "events.jsonl"), "-f", str(EX / "proposed.json"), "-k", str(EX / "must-keep.json")], capsys)
    assert code == 1
    assert "MUST-KEEP EVENTS THAT WOULD BE DROPPED: 2" in out


def test_cli_clean_config_passes(capsys):
    code, out, _ = run(["run", str(EX / "events.jsonl"), "--try", "error_message:*ResizeObserver*", "-k", str(EX / "must-keep.json")], capsys)
    assert code == 0 and "no must-keep events dropped" in out


def test_cli_max_drop_and_fail_on_new(capsys):
    args = ["run", str(EX / "events.jsonl"), "--try", "error_message:*ResizeObserver*"]
    assert run(args + ["--max-drop", "50"], capsys)[0] == 0
    code, _, err = run(args + ["--max-drop", "10"], capsys)
    assert code == 1 and "above --max-drop" in err
    base = ["--baseline", str(EX / "current.json"), "--fail-on-new"]
    assert run(args + base, capsys)[0] == 0  # current config already drops ResizeObserver
    assert run(["run", str(EX / "events.jsonl"), "-f", str(EX / "proposed.json")] + base, capsys)[0] == 1


def test_cli_json_and_markdown(capsys):
    code, out, _ = run(["run", str(EX / "events.jsonl"), "-f", str(EX / "proposed.json"), "--format", "json"], capsys)
    data = json.loads(out)
    assert code == 0 and data["events"] == 16 and data["dropped_events"] == 11
    assert any("web crawlers" in n for n in data["notes"])
    code, out, _ = run(["run", str(EX / "events.jsonl"), "-f", str(EX / "proposed.json"), "-k", str(EX / "must-keep.json"), "--format", "markdown"], capsys)
    assert code == 1 and out.startswith("### filter-rehearsal") and "| filter | events | issues |" in out


def test_cli_explain(capsys):
    code, out, _ = run(["explain", str(EX / "events.jsonl"), "--event-id", "ae87", "--try", "error_type:a.b.c"], capsys)
    assert code == 0 and "DROP  custom" in out and "a.b.c: cart is empty" in out
    code, _, err = run(["explain", str(EX / "events.jsonl"), "--event-id", "zzzz", "--try", "error_type:x"], capsys)
    assert code == 2 and "no event id" in err


def test_cli_usage_errors(capsys, tmp_path):
    code, _, err = run(["run", str(EX / "events.jsonl")], capsys)
    assert code == 2 and "no filters given" in err
    code, _, err = run(["run", str(tmp_path / "nope.json"), "--try", "release:x"], capsys)
    assert code == 2 and "no such file" in err
    empty = tmp_path / "e.jsonl"
    empty.write_text("")
    code, _, err = run(["run", str(empty), "--try", "release:x"], capsys)
    assert code == 2 and "no events" in err


# ---- fetch against a local fake Sentry ----------------------------------
class FakeSentry(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *a):  # quiet
        pass

    def _send(self, body, link=None, status=200):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        if link:
            self.send_header("Link", link)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        FakeSentry.seen.append((self.path, self.headers.get("Authorization")))
        base = f"http://127.0.0.1:{self.server.server_port}"
        if self.headers.get("Authorization") != "Bearer tok":
            return self._send({"detail": "bad token"}, status=401)
        if self.path.startswith("/api/0/projects/acme/web/events/"):
            if "cursor=2" in self.path:
                nxt = f'<{base}/api/0/projects/acme/web/events/?cursor=3>; rel="next"; results="false"; cursor="3"'
                return self._send([{"eventID": "e3"}], link=nxt)
            nxt = f'<{base}/api/0/projects/acme/web/events/?cursor=1>; rel="previous"; results="false"; cursor="1", <{base}/api/0/projects/acme/web/events/?full=true&cursor=2>; rel="next"; results="true"; cursor="2"'
            return self._send([{"eventID": "e1"}, {"eventID": "e2"}], link=nxt)
        if self.path == "/api/0/projects/acme/web/":
            return self._send({"slug": "web", "options": {"filters:releases": "web@1.*", "filters:error_messages": "*x*", "sentry:other": 1}})
        if self.path == "/api/0/projects/acme/web/filters/":
            return self._send([{"id": "localhost", "active": True, "description": "..."}, {"id": "web-crawlers", "active": False}])
        self._send({"detail": "not found"}, status=404)


@pytest.fixture
def fake_sentry():
    srv = HTTPServer(("127.0.0.1", 0), FakeSentry)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    FakeSentry.seen = []
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_fetch_events_and_filters(fake_sentry, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SENTRY_AUTH_TOKEN", "tok")
    ev, fl = tmp_path / "ev.jsonl", tmp_path / "current.json"
    code = main(["fetch", "--org", "acme", "--project", "web", "--base-url", fake_sentry, "--events-out", str(ev), "--filters-out", str(fl)])
    assert code == 0
    assert [json.loads(line)["eventID"] for line in ev.read_text().splitlines()] == ["e1", "e2", "e3"]
    assert "full=true" in FakeSentry.seen[0][0] and "statsPeriod=14d" in FakeSentry.seen[0][0]
    cfg = json.loads(fl.read_text())
    assert cfg == {
        "data_filters": [{"active": True, "id": "localhost"}, {"active": False, "id": "web-crawlers"}],
        "filters:error_messages": "*x*",
        "filters:releases": "web@1.*",
    }
    # the written file is a valid config for --filters / --baseline
    loaded = config_from_obj(cfg)
    assert sorted(r.kind for r in loaded.rules) == ["error_messages", "localhost", "releases"]


def test_fetch_limit_and_errors(fake_sentry, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SENTRY_AUTH_TOKEN", "tok")
    ev = tmp_path / "ev.jsonl"
    assert main(["fetch", "--org", "acme", "--project", "web", "--base-url", fake_sentry, "--events-out", str(ev), "--limit", "1"]) == 0
    assert len(ev.read_text().splitlines()) == 1
    monkeypatch.setenv("SENTRY_AUTH_TOKEN", "wrong")
    assert main(["fetch", "--org", "acme", "--project", "web", "--base-url", fake_sentry, "--events-out", str(ev)]) == 1
    assert "HTTP 401" in capsys.readouterr().err
    monkeypatch.delenv("SENTRY_AUTH_TOKEN")
    assert main(["fetch", "--org", "acme", "--project", "web", "--events-out", str(ev)]) == 2
