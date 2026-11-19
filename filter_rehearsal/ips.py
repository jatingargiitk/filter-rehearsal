"""IP / CIDR matching for inbound IP filters (stdlib ipaddress).

Accepts the address formats listed in the Sentry inbound-filters docs:
IPv4, IPv6, IPv4 CIDR, IPv6 CIDR.
"""

from __future__ import annotations

import ipaddress
from typing import Iterable, Optional, Union

Network = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]


class IPError(ValueError):
    pass


def parse_network(value: str) -> Network:
    text = (value or "").strip()
    if not text:
        raise IPError("empty IP / CIDR value")
    try:
        if "/" in text:
            return ipaddress.ip_network(text, strict=False)
        addr = ipaddress.ip_address(text)
        return ipaddress.ip_network(addr)
    except ValueError as exc:
        raise IPError(f"not a valid IP address or CIDR: {value!r}") from exc


def ip_in(addr: Optional[str], networks: Iterable[Network]) -> bool:
    if not addr:
        return False
    try:
        ip = ipaddress.ip_address(addr.strip())
    except ValueError:
        return False
    for net in networks:
        # IPv4-mapped IPv6 is compared as IPv4 when the network is IPv4
        if ip.version != net.version:
            if ip.version == 6 and getattr(ip, "ipv4_mapped", None) is not None:
                ip4 = ip.ipv4_mapped
                if ip4 in net:  # type: ignore[operator]
                    return True
            continue
        if ip in net:
            return True
    return False
