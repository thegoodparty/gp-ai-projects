"""Shared SSRF guard for broker HTTP/PDF fetch endpoints.

Single canonical implementation used by `broker/endpoints/http_fetch.py` and
`broker/endpoints/pdf_fetch.py`. Both previously duplicated these helpers
byte-for-byte; any future security fix must apply to both at once.

KNOWN LIMITATION: DNS rebinding TOCTOU. We resolve + validate once, then
httpx re-resolves on connect. A short-TTL attacker-controlled authoritative
nameserver can return a public IP for the first lookup and 169.254.169.254
for the second. Proper fix requires pinning the resolved IP through a custom
httpx.AsyncHTTPTransport/httpcore resolver (tracked in broker/ARCHITECTURE.md
open items; batch 1b followup). Until then, the egress SG restricting broker
tasks to AWS-listed outbound destinations is the primary defense.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse

from fastapi import HTTPException


async def validate_url(url: str) -> None:
    """SSRF guard. Block requests to private, loopback, link-local, and
    metadata endpoints."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise HTTPException(status_code=400, detail="URL must use https scheme")
    host = parsed.hostname
    if not host:
        raise HTTPException(status_code=400, detail="URL missing hostname")

    low = host.lower()
    if low == "localhost" or low.endswith(".localhost"):
        raise HTTPException(status_code=400, detail="Loopback hostname not allowed")

    try:
        ip = ipaddress.ip_address(host)
        reject_if_private(ip)
        return
    except ValueError:
        pass

    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, parsed.port or 443, proto=socket.IPPROTO_TCP
        )
    except socket.gaierror as e:
        raise HTTPException(status_code=400, detail=f"DNS resolution failed: {e}")

    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        reject_if_private(ip)


def reject_if_private(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        reject_if_private(ip.ipv4_mapped)
        return

    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        raise HTTPException(
            status_code=400,
            detail=f"URL resolves to blocked address range: {ip}",
        )
