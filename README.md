# filter-rehearsal

Rehearse Sentry inbound filters against events you already have, before you turn them on.

Sentry drops filtered events at ingest. They don't count against quota, and you can't get them back. So if you add `*Failed to fetch*` to silence browser noise and it also matches the checkout page's payment call, you lose those errors without any warning. Sentry shows you how many events a filter dropped, but not which ones.

`filter-rehearsal` runs a proposed filter config over a sample of real events (exported JSON, or pulled read-only from the API). It shows what would be dropped, by which rule and from which issues. If anything you marked **must-keep** would go, it exits 1, so a filter change can be reviewed in a PR like any other config change.

- Covers the built-in lists (error messages, releases, IP addresses), the localhost and health-check filters, and custom inbound filters with every documented operator (`equals`/glob, `contains`, `starts_with`, `ends_with`, the negated forms, release `<`/`<=`/`>`/`>=`, `cidr`/`not_cidr`).
- `--baseline current.json` shows what the new config drops that the current one doesn't.
- `explain` shows the exact string each filter sees in one event, for when a filter "doesn't work".
- Python 3.9+, standard library only. No Sentry SDK needed.

Not affiliated with Sentry. The matching rules are re-implemented from the public [Inbound Filters docs](https://docs.sentry.io/concepts/data-management/filtering/), not from Relay's code. See [What it can and can't rehearse](#what-it-can-and-cant-rehearse).

## Install

```bash
pip install git+https://github.com/jatingargiitk/filter-rehearsal
```

## Quick start

`examples/` has 16 synthetic events from a fictional shop (browser errors, an obfuscated Android crash, API transactions). It also has a proposed filter config:

```json
{
  "error_messages": [
    "*ResizeObserver loop*",
    "*Failed to fetch*"
  ],
  "releases": [
    "shop-web@1.*"
  ],
  "blacklisted_ips": [
    "10.8.0.0/16"
  ],
  "localhost": true,
  "health_check": true,
  "web-crawlers": true
}
```

and the events you never want to lose:

```json
[
  {
    "label": "checkout",
    "tag": {
      "flow": "checkout"
    }
  },
  {
    "label": "enterprise",
    "tag": {
      "customer.tier": "enterprise"
    }
  },
  {
    "label": "payments",
    "error_type": "Payment*"
  }
]
```

```console
$ filter-rehearsal run examples/events.jsonl -f examples/proposed.json -k examples/must-keep.json
16 events in 13 issues/transactions
would drop 11 events (68.8%): 8 fully, 0 partly

by filter
  error_messages "*ResizeObserver loop*"  3 events   1 issue
  error_messages "*Failed to fetch*"      2 events  2 issues
  releases "shop-web@1.*"                 2 events   1 issue
  blacklisted_ips 10.8.0.0/16              1 event   1 issue
  localhost                                1 event   1 issue
  health_check                            2 events  2 issues

issues hit
  all  3/3  #5101 Error: ResizeObserver loop completed with undelivered notif…  <- error_messages "*ResizeObserver loop*"
  all  1/1  #5102 TypeError: Failed to fetch  <- error_messages "*Failed to fetch*"
  all  1/1  #5103 TypeError: Failed to fetch  <- error_messages "*Failed to fetch*"
  all  2/2  #5106 TypeError: Cannot read properties of undefined (reading 'pr…  <- releases "shop-web@1.*"
  all  1/1  #5107 TypeError: Cannot read properties of undefined (reading 'it…  <- localhost
  all  1/1  #5110 Error: Network request failed: timeout of 10000ms exceeded  <- blacklisted_ips 10.8.0.0/16
  all  1/1  GET /api/healthz  <- health_check
  all  1/1  GET /api/ping  <- health_check

MUST-KEEP EVENTS THAT WOULD BE DROPPED: 2
  91d05480 [checkout] TypeError: Failed to fetch
      by error_messages "*Failed to fetch*": matched 'TypeError: Failed to fetch'
  c41e4b19 [enterprise] TypeError: Cannot read properties of undefined (reading 'pr…
      by releases "shop-web@1.*": release 'shop-web@1.9.3'

notes
  - IP filters use the IP the request came from; exports do not keep it, so user.ip_address (or _client_ip) is used (16 of 16 events used user.ip_address, 0 had no IP)
  - localhost: matches loopback IPs (127.0.0.0/8, ::1) or a request URL host of localhost/*.localhost/127.x/::1
  - not rehearsed: web crawlers is switched on, but its match list is not documented, so its drops are not counted
$ echo $?
1
```

Both catches are realistic. `*Failed to fetch*` silences the product page's flaky fetch, but it also silences the checkout payment call. `shop-web@1.*` retires old releases, and an enterprise customer is still on 1.9.3. The other rules are safe.

The web-crawler filter is on in the config, but its match list isn't published in the docs. So it's reported as not rehearsed instead of being guessed at.

## Try a custom filter before creating it

`--try` takes the same `key:value` syntax as the custom filter UI (`key:[a,b]` for several values, `release:<2.0.0` for version comparisons, `ip_address:10.0.0.0/8`). Conditions are ANDed.

```console
$ filter-rehearsal run examples/events.jsonl --try 'error_type:TypeError release:<2.0.0' -k examples/must-keep.json
16 events in 13 issues/transactions
would drop 2 events (12.5%): 1 fully, 0 partly

by filter
  custom "error_type:TypeError release:<2.0.0"  2 events  1 issue

issues hit
  all  2/2  #5106 TypeError: Cannot read properties of undefined (reading 'pr…  <- custom "error_type:TypeError release:<2.0.0"

MUST-KEEP EVENTS THAT WOULD BE DROPPED: 1
  c41e4b19 [enterprise] TypeError: Cannot read properties of undefined (reading 'pr…
      by custom "error_type:TypeError release:<2.0.0": error_type:equals:TypeError & release:lt:2.0.0
$ echo $?
1
```

## Compare with what's on now (PR comment)

`--baseline` takes your current settings, so the report shows only what changes. `--format markdown` is ready to post as a PR comment. `--fail-on-new` exits 1 if anything is newly dropped.

```console
$ filter-rehearsal run examples/events.jsonl -f examples/proposed.json --baseline examples/current.json --fail-on-new --format markdown
```

```markdown
### filter-rehearsal

**11 of 16 events dropped (68.8%)**, 8 issues/transactions fully, 0 partly.

No must-keep events dropped.

**vs baseline:** 7 newly dropped, 0 no longer dropped (baseline drops 4).

| newly dropped | title | by |
|---|---|---|
| `f2bc6eb2` | TypeError: Failed to fetch | error_messages "*Failed to fetch*" |
| `91d05480` | TypeError: Failed to fetch | error_messages "*Failed to fetch*" |
| `c41e4b19` | TypeError: Cannot read properties of undefined (reading 'price') | releases "shop-web@1.*" |
| `2fc6f7b3` | TypeError: Cannot read properties of undefined (reading 'price') | releases "shop-web@1.*" |
| `93c9a236` | Error: Network request failed: timeout of 10000ms exceeded | blacklisted_ips 10.8.0.0/16 |
| `e40b22f5` | GET /api/healthz | health_check |
| `d722eeb2` | GET /api/ping | health_check |

| filter | events | issues |
|---|---:|---:|
| error_messages "*ResizeObserver loop*" | 3 | 1 |
| error_messages "*Failed to fetch*" | 2 | 2 |
| releases "shop-web@1.*" | 2 | 1 |
| blacklisted_ips 10.8.0.0/16 | 1 | 1 |
| localhost | 1 | 1 |
| health_check | 2 | 2 |

- IP filters use the IP the request came from; exports do not keep it, so user.ip_address (or _client_ip) is used (16 of 16 events used user.ip_address, 0 had no IP)
- localhost: matches loopback IPs (127.0.0.0/8, ::1) or a request URL host of localhost/*.localhost/127.x/::1
- not rehearsed: web crawlers is switched on, but its match list is not documented, so its drops are not counted
```

## Why doesn't my filter match? (`explain`)

Inbound filters run **before** deobfuscation and symbolication. An Android crash titled `com.shop.cart.CartStateException` reached Sentry as `a.b.c`, so a pattern on the readable name never fires:

```console
$ filter-rehearsal explain examples/events.jsonl --event-id ae87 --try 'error_message:*CartStateException*' --try 'error_type:a.b.c'
event ae87203d243aefe737e68ce767f32949 (error) from examples/events.jsonl
  title: com.shop.cart.CartStateException: cart is empty at checkout
  error messages list sees: 'a.b.c: cart is empty at checkout'
  release: 'shop-android@5.1.0'  ip: '203.0.113.10' (user.ip_address)  country: 'US'

  pass  custom "error_message:*CartStateException*"
  DROP  custom "error_type:a.b.c"  (error_type:equals:a.b.c)
$ echo $?
0
```

## Getting events and the current config

Export events however you like. `run` reads a single event (the "View JSON" download), a JSON array, JSONL, or a directory of those. It accepts both the event-JSON shape and the events-API shape (`entries[]`).

Or pull them read-only (needs a token with `project:read`):

```bash
export SENTRY_AUTH_TOKEN=...
filter-rehearsal fetch --org acme --project web --period 14d --limit 2000 \
  --events-out events.jsonl --filters-out current.json
```

`fetch` calls `GET /api/0/projects/{org}/{project}/events/?full=true` and follows the `Link` cursor. It also calls `GET /api/0/projects/{org}/{project}/` (the `filters:*` options) and `.../filters/` (the built-in toggles). `--base-url` works for self-hosted and regional hosts (`https://de.sentry.io`). It never writes to Sentry. The test suite exercises it against a local fake server, not against sentry.io.

## Config format

`--filters` (repeatable) accepts any of these:

- the format above: `error_messages`, `releases`, `blacklisted_ips` (lists, or newline-separated strings like the settings page), `localhost`, `health_check`, and `custom` (a list of custom filters in the API's shape: `{"name", "dataType", "conditions": [{"type", "operator", "value": [...]}]}`);
- a project details response, or its `options` (`filters:error_messages`, `filters:releases`, `filters:blacklisted_ips`);
- a data filters list, `[{"id": "localhost", "active": true}, ...]`, or that list under `data_filters` (which is what `fetch --filters-out` writes).

Must-keep rules (`-k`, repeatable) are a list of objects. Keys within one object are ANDed; an event matches if any object matches. Available keys: `title`, `message`, `error_type`, `release`, `level`, `issue`, `event_id`, `transaction`, and `tag` (`{"tag": {"customer.tier": "enterprise"}}`). Values are case-insensitive globs. `label` names the rule in the report.

Exit codes: `0` ok, `1` a must-keep event would be dropped (or `--max-drop PCT` was exceeded, or `--fail-on-new` found new drops), `2` bad input.

## Matching rules (from the docs)

- Globs are case-insensitive. `*` matches any run of characters and `?` matches one character. `\` escapes, and `[...]`/`[!...]` are character classes. The whole string must match, so `*panic*` means "contains". The docs call `{}` reserved; this tool reads `{a,b}` as alternation, as bash does.
- The **Error Messages** list matches `{exception.type}: {exception.value}` (every exception in a chain), or the formatted message for message events. It never matches transactions. It uses the type as received (`raw_type`), not the deobfuscated one.
- **Releases** match the full release name (`package@version`) with a glob, with no SemVer handling. They never match an event without a release.
- **Custom filters**: `error_message` checks the type, value and message separately, so `TypeError: *` only works in the Error Messages list. `equals` is exact unless the value has `*`. A list of values matches any of them. Negated operators also match events that lack the property. Release comparisons follow the documented version rules: up to 4 numeric parts, pre-releases sort below their final release, build metadata is ignored, a package in the value must match, and a commit-hash release never compares. A filter made only of IP conditions drops every event type.
- **Health checks**: transactions whose name matches one of the 14 documented patterns (`*/healthz`, `*/ping`, `*heartbeat*`, ...).

## What it can and can't rehearse

- **IP filters** use the IP of the HTTP request that delivered the event, and exports don't record it. `filter-rehearsal` uses `user.ip_address` instead, which is what the SDK's `{{auto}}` / `sendDefaultPii` resolves to. You can also set `_client_ip` on an event yourself. The report says how many events relied on this.
- **Localhost** is not specified in the docs. Here it means a loopback IP (`127.0.0.0/8`, `::1`), or a request URL whose host is `localhost`, `*.localhost` or a loopback address.
- **Browser extensions, web crawlers, legacy browsers, React hydration errors and ChunkLoadErrors** are reported as *not rehearsed* when they're on. Their match lists aren't in the docs, and they aren't copied from Relay.
- Log, metric and span filters, Allowed Domains, rate limits and SDK-side `ignoreErrors`/`beforeSend` are out of scope.
- A rehearsal is only as good as the sample. Events that a filter already drops never reach your export.

## Development

```bash
pip install -e '.[test]'
pytest -q
./examples/readme_examples.sh   # re-records every output shown above
```

CI runs the tests on Python 3.9 to 3.13. It also re-runs `examples/readme_examples.sh` and fails if any recorded output changed. `tests/test_readme.py` checks that each recorded output appears verbatim in this README.

## License

MIT
