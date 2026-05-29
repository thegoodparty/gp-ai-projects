import os
import socket
from urllib.parse import urlparse

MESSAGE = (
    "Sandboxed environment: this container has NO direct network egress. "
    "Direct network calls (urllib, requests, httpx, curl, wget, raw socket) "
    "cannot reach the internet and will fail. The ONLY way to reach a URL is "
    "the broker-proxied tools you were given: pmf_runtime.http.head(url) / "
    "pmf_runtime.http.get(url) / pmf_runtime.http.download(url) for web pages, "
    "and the WebSearch tool for discovery. Use those instead. "
    "See /workspace/SANDBOX.md."
)

_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1", None})

_installed = False
_orig_getaddrinfo = None
_orig_connect = None


class SandboxEgressError(OSError):
    pass


def _is_loopback_ip(ip: str) -> bool:
    return ip == "127.0.0.1" or ip == "::1" or ip.startswith("127.") or ip == "::ffff:127.0.0.1"


def install(broker_url: str | None = None) -> None:
    global _installed, _orig_getaddrinfo, _orig_connect

    resolved_url = broker_url or os.environ.get("BROKER_URL")
    if not resolved_url:
        return
    if _installed:
        return

    broker_host = urlparse(resolved_url).hostname

    _orig_getaddrinfo = socket.getaddrinfo
    _orig_connect = socket.socket.connect

    broker_ips: set[str] = set()
    if broker_host is not None:
        try:
            for info in _orig_getaddrinfo(broker_host, None):
                broker_ips.add(info[4][0])
        except OSError:
            pass

    allowed_hosts = set(_LOOPBACK)
    allowed_hosts.add(broker_host)

    def guarded_getaddrinfo(host, *args, **kwargs):
        if host not in allowed_hosts:
            raise SandboxEgressError(MESSAGE)
        return _orig_getaddrinfo(host, *args, **kwargs)

    def guarded_connect(self, address):
        ip = address[0] if isinstance(address, (tuple, list)) and address else None
        if isinstance(ip, str) and not _is_loopback_ip(ip) and ip not in broker_ips:
            raise SandboxEgressError(MESSAGE)
        return _orig_connect(self, address)

    socket.getaddrinfo = guarded_getaddrinfo
    socket.socket.connect = guarded_connect
    _installed = True


def uninstall() -> None:
    global _installed, _orig_getaddrinfo, _orig_connect
    if not _installed:
        return
    socket.getaddrinfo = _orig_getaddrinfo
    socket.socket.connect = _orig_connect
    _orig_getaddrinfo = None
    _orig_connect = None
    _installed = False
