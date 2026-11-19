from filter_rehearsal.events import Event


def error(typ="TypeError", value="boom", release="web@1.0.0", ip=None, url=None, tags=None, **extra):
    raw = {"event_id": extra.pop("event_id", "0" * 32), "type": "error"}
    if typ is not None or value is not None:
        raw["exception"] = {"values": [{"type": typ, "value": value}]}
    if release is not None:
        raw["release"] = release
    if ip is not None:
        raw["user"] = {"ip_address": ip}
    if url is not None:
        raw["request"] = {"url": url}
    if tags:
        raw["tags"] = [[k, v] for k, v in tags.items()]
    raw.update(extra)
    return Event(raw)


def transaction(name, release="api@1.0.0", ip=None):
    raw = {"event_id": "1" * 32, "type": "transaction", "transaction": name, "release": release}
    if ip:
        raw["user"] = {"ip_address": ip}
    return Event(raw)
