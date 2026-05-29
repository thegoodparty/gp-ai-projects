import os
import socket
import sys
from urllib.parse import urlparse

MESSAGE = (
    "Sandboxed environment: this container has NO direct network egress. "
    "Direct network calls (urllib, requests, httpx, curl, wget, raw socket) "
    "cannot reach the internet and ALWAYS fail here. "
    "STOP. Do NOT waste turns inspecting, importing, or reverse-engineering the "
    "runtime to figure out how to make a request. Use this exact, ready-to-run path:\n"
    "  from pmf_runtime import http\n"
    "  r = http.head(url)   # -> {'status': int, 'final_url': str}; cite the URL only if status == 200\n"
    "Verify URLs with pmf_runtime.http.head FIRST. ONLY if http.head fails for a URL you believe is real "
    "(e.g. 403/405 from a bot-walled site, or you need the page body) escalate to the browser:\n"
    "  r = http.get(url)    # -> {'status': int, 'body': str, 'source_url': str}\n"
    "For discovering candidates/URLs use the WebSearch tool. Never retry urllib/curl. "
    "See /workspace/SANDBOX.md."
)

_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1", None})

_installed = False
_orig_getaddrinfo = None
_orig_connect = None
_orig_connect_ex = None


class SandboxEgressError(OSError):
    pass


def _is_loopback_ip(ip: str) -> bool:
    return ip == "127.0.0.1" or ip == "::1" or ip.startswith("127.") or ip == "::ffff:127.0.0.1"


def install(broker_url: str | None = None) -> None:
    global _installed, _orig_getaddrinfo, _orig_connect, _orig_connect_ex

    resolved_url = broker_url or os.environ.get("BROKER_URL")
    if not resolved_url:
        return
    if _installed:
        return

    broker_host = urlparse(resolved_url).hostname

    _orig_getaddrinfo = socket.getaddrinfo
    _orig_connect = socket.socket.connect
    _orig_connect_ex = socket.socket.connect_ex

    broker_ips: set[str] = set()
    if broker_host is not None:
        try:
            for info in _orig_getaddrinfo(broker_host, None):
                broker_ips.add(info[4][0])
        except OSError as exc:
            print(f"egress_guard: broker host pre-resolve failed for {broker_host!r}: {exc}", file=sys.stderr)

    allowed_hosts = set(_LOOPBACK)
    allowed_hosts.add(broker_host)

    def guarded_getaddrinfo(host, *args, **kwargs):
        if host not in allowed_hosts:
            raise SandboxEgressError(MESSAGE)
        results = _orig_getaddrinfo(host, *args, **kwargs)
        if host == broker_host:
            for info in results:
                broker_ips.add(info[4][0])
        return results

    def _reject_if_blocked(address):
        ip = address[0] if isinstance(address, (tuple, list)) and address else None
        if isinstance(ip, str) and not _is_loopback_ip(ip) and ip not in broker_ips:
            raise SandboxEgressError(MESSAGE)

    def guarded_connect(self, address):
        _reject_if_blocked(address)
        return _orig_connect(self, address)

    def guarded_connect_ex(self, address):
        _reject_if_blocked(address)
        return _orig_connect_ex(self, address)

    socket.getaddrinfo = guarded_getaddrinfo
    socket.socket.connect = guarded_connect
    socket.socket.connect_ex = guarded_connect_ex
    _installed = True


def uninstall() -> None:
    global _installed, _orig_getaddrinfo, _orig_connect, _orig_connect_ex
    if not _installed:
        return
    socket.getaddrinfo = _orig_getaddrinfo
    socket.socket.connect = _orig_connect
    socket.socket.connect_ex = _orig_connect_ex
    _orig_getaddrinfo = None
    _orig_connect = None
    _orig_connect_ex = None
    _installed = False
