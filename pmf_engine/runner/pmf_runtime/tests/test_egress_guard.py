import socket

import pytest

from pmf_engine.runner.pmf_runtime import egress_guard
from pmf_engine.runner.pmf_runtime.egress_guard import SandboxEgressError


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    egress_guard.uninstall()


class TestGetaddrinfoGuard:
    def test_blocks_non_allowlisted_host(self, monkeypatch):
        calls = []

        def fake_getaddrinfo(host, *args, **kwargs):
            calls.append(host)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.2.3.4", 0))]

        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

        egress_guard.install("https://broker.example.com")

        with pytest.raises(SandboxEgressError) as exc:
            socket.getaddrinfo("evil.com", 443)

        msg = str(exc.value)
        assert "no direct network egress" in msg.lower()
        assert "pmf_runtime.http" in msg
        assert "evil.com" not in calls

    def test_allows_broker_host(self, monkeypatch):
        calls = []

        def fake_getaddrinfo(host, *args, **kwargs):
            calls.append(host)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.2.3.4", 0))]

        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

        egress_guard.install("https://broker.example.com")

        socket.getaddrinfo("broker.example.com", 443)
        assert "broker.example.com" in calls

    def test_allows_localhost(self, monkeypatch):
        calls = []

        def fake_getaddrinfo(host, *args, **kwargs):
            calls.append(host)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]

        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

        egress_guard.install("https://broker.example.com")

        socket.getaddrinfo("localhost", 80)
        assert "localhost" in calls


class TestConnectGuard:
    def test_blocks_non_loopback_ip_literal(self, monkeypatch):
        def fake_getaddrinfo(host, *args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.9.9.9", 0))]

        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

        connect_calls = []
        monkeypatch.setattr(socket.socket, "connect", lambda self, address: connect_calls.append(address))

        egress_guard.install("https://broker.example.com")

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with pytest.raises(SandboxEgressError):
                s.connect(("8.8.8.8", 53))
        finally:
            s.close()
        assert ("8.8.8.8", 53) not in connect_calls

    def test_blocks_non_loopback_ip_literal_via_connect_ex(self, monkeypatch):
        """connect_ex is a distinct C-level method — a caller using it with an IP
        literal must be blocked too, not just connect()."""
        def fake_getaddrinfo(host, *args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.9.9.9", 0))]

        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
        ex_calls = []
        monkeypatch.setattr(socket.socket, "connect_ex", lambda self, address: ex_calls.append(address) or 0)

        egress_guard.install("https://broker.example.com")

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with pytest.raises(SandboxEgressError):
                s.connect_ex(("8.8.8.8", 53))
        finally:
            s.close()
        assert ("8.8.8.8", 53) not in ex_calls

    def test_allows_loopback_ip_literal(self, monkeypatch):
        def fake_getaddrinfo(host, *args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.9.9.9", 0))]

        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

        connect_calls = []
        monkeypatch.setattr(socket.socket, "connect", lambda self, address: connect_calls.append(address))

        egress_guard.install("https://broker.example.com")

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.connect(("127.0.0.1", 4001))
        finally:
            s.close()
        assert ("127.0.0.1", 4001) in connect_calls

    def test_allows_broker_resolved_ip_on_connect(self, monkeypatch):
        def fake_getaddrinfo(host, *args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.9.9.9", 0))]

        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

        connect_calls = []
        monkeypatch.setattr(socket.socket, "connect", lambda self, address: connect_calls.append(address))

        egress_guard.install("https://broker.example.com")

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.connect(("10.9.9.9", 443))
        finally:
            s.close()
        assert ("10.9.9.9", 443) in connect_calls

    def test_allows_rotated_broker_ip(self, monkeypatch):
        current_ip = ["10.9.9.9"]

        def fake_getaddrinfo(host, *args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (current_ip[0], 0))]

        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

        connect_calls = []
        monkeypatch.setattr(socket.socket, "connect", lambda self, address: connect_calls.append(address))

        egress_guard.install("https://broker.example.com")

        current_ip[0] = "10.8.8.8"
        socket.getaddrinfo("broker.example.com", 443)

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.connect(("10.8.8.8", 443))
        finally:
            s.close()
        assert ("10.8.8.8", 443) in connect_calls


class TestNoOp:
    def test_no_broker_url_is_noop(self, monkeypatch):
        monkeypatch.delenv("BROKER_URL", raising=False)

        def fake_getaddrinfo(host, *args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.2.3.4", 0))]

        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

        egress_guard.install()

        socket.getaddrinfo("evil.com", 443)


class TestUninstall:
    def test_uninstall_restores_original(self, monkeypatch):
        original = socket.getaddrinfo
        egress_guard.install("https://broker.example.com")
        assert socket.getaddrinfo is not original
        egress_guard.uninstall()
        assert socket.getaddrinfo is original
