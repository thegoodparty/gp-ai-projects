import ipaddress
import socket
from urllib.parse import urlparse

BLOCKED_IP_RANGES = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),  # AWS metadata
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fd00::/8"),
    ipaddress.ip_network("fe80::/10"),
]


def validate_url(url: str) -> None:
    """Validate URL is safe to render (SSRF protection)."""
    parsed = urlparse(url)

    # Only allow http and https schemes
    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"URL scheme '{parsed.scheme}' is not allowed. Only http and https are permitted."
        )

    # Resolve hostname and check against blocked IP ranges
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("URL has no hostname")

    try:
        addrinfo = socket.getaddrinfo(hostname, None)
        for family, type_, proto, canonname, sockaddr in addrinfo:
            ip = ipaddress.ip_address(sockaddr[0])
            for blocked in BLOCKED_IP_RANGES:
                if ip in blocked:
                    raise ValueError("URL resolves to blocked IP range")
    except socket.gaierror:
        raise ValueError(f"Cannot resolve hostname: {hostname}")
