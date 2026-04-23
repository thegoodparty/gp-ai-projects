from __future__ import annotations

import ipaddress
import socket
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException

from broker.ssrf_guard import reject_if_private, resolve_redirects, validate_url


def _fake_getaddrinfo_factory(ip_literal: str):
    async def fake(host, port, proto=0):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip_literal, port)),
        ]

    return fake


class TestValidateUrlSchemeAndHostname:
    @pytest.mark.asyncio
    async def test_validate_url_rejects_file_scheme(self):
        with pytest.raises(HTTPException) as exc:
            await validate_url("file:///etc/passwd")
        assert exc.value.status_code == 400
        assert "https" in exc.value.detail.lower()

    @pytest.mark.asyncio
    async def test_validate_url_rejects_http_scheme(self):
        with pytest.raises(HTTPException) as exc:
            await validate_url("http://example.com/")
        assert exc.value.status_code == 400
        assert "https" in exc.value.detail.lower()

    @pytest.mark.asyncio
    async def test_validate_url_rejects_missing_hostname(self):
        with pytest.raises(HTTPException) as exc:
            await validate_url("https:///path")
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_validate_url_rejects_loopback_hostname(self):
        with pytest.raises(HTTPException) as exc:
            await validate_url("https://localhost/")
        assert exc.value.status_code == 400
        assert "loopback" in exc.value.detail.lower()

    @pytest.mark.asyncio
    async def test_validate_url_rejects_subdomain_localhost(self):
        with pytest.raises(HTTPException) as exc:
            await validate_url("https://app.localhost/")
        assert exc.value.status_code == 400
        assert "loopback" in exc.value.detail.lower()


class TestValidateUrlIPLiterals:
    @pytest.mark.asyncio
    async def test_validate_url_rejects_loopback_ip_literal(self):
        with pytest.raises(HTTPException) as exc:
            await validate_url("https://127.0.0.1/")
        assert exc.value.status_code == 400
        assert "blocked" in exc.value.detail.lower()

    @pytest.mark.asyncio
    async def test_validate_url_rejects_link_local_metadata(self):
        with pytest.raises(HTTPException) as exc:
            await validate_url("https://169.254.169.254/latest/meta-data/")
        assert exc.value.status_code == 400
        assert "blocked" in exc.value.detail.lower()

    @pytest.mark.asyncio
    async def test_validate_url_rejects_ipv6_loopback(self):
        with pytest.raises(HTTPException) as exc:
            await validate_url("https://[::1]/")
        assert exc.value.status_code == 400
        assert "blocked" in exc.value.detail.lower()

    @pytest.mark.asyncio
    async def test_validate_url_rejects_private_ipv4_literal(self):
        with pytest.raises(HTTPException) as exc:
            await validate_url("https://10.0.0.5/")
        assert exc.value.status_code == 400
        assert "blocked" in exc.value.detail.lower()


class TestValidateUrlDNSResolution:
    @pytest.mark.asyncio
    async def test_validate_url_resolves_dns_and_rejects_private(self):
        """Public-looking hostname resolves to a private IP - must be rejected."""

        async def fake_getaddrinfo(host, port, proto=0):
            return [
                (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("10.0.0.5", port)),
            ]

        with patch("asyncio.get_running_loop") as mock_loop:
            mock_loop.return_value.getaddrinfo = fake_getaddrinfo
            with pytest.raises(HTTPException) as exc:
                await validate_url("https://evil.example.com/")
            assert exc.value.status_code == 400
            assert "blocked" in exc.value.detail.lower()

    @pytest.mark.asyncio
    async def test_validate_url_accepts_public_http(self):
        """Public hostname resolving to a public IP passes."""

        async def fake_getaddrinfo(host, port, proto=0):
            return [
                (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", port)),
            ]

        with patch("asyncio.get_running_loop") as mock_loop:
            mock_loop.return_value.getaddrinfo = fake_getaddrinfo
            await validate_url("https://example.com/path")

    @pytest.mark.asyncio
    async def test_validate_url_raises_on_dns_failure(self):
        async def fake_getaddrinfo(host, port, proto=0):
            raise socket.gaierror("nodename nor servname provided")

        with patch("asyncio.get_running_loop") as mock_loop:
            mock_loop.return_value.getaddrinfo = fake_getaddrinfo
            with pytest.raises(HTTPException) as exc:
                await validate_url("https://unresolvable.example.com/")
            assert exc.value.status_code == 400
            assert "dns" in exc.value.detail.lower()


class TestRejectIfPrivate:
    def test_reject_if_private_rejects_ipv4_mapped_ipv6(self):
        ip = ipaddress.ip_address("::ffff:127.0.0.1")
        with pytest.raises(HTTPException) as exc:
            reject_if_private(ip)
        assert exc.value.status_code == 400

    def test_reject_if_private_rejects_ipv4_mapped_metadata(self):
        ip = ipaddress.ip_address("::ffff:169.254.169.254")
        with pytest.raises(HTTPException) as exc:
            reject_if_private(ip)
        assert exc.value.status_code == 400

    def test_reject_if_private_rejects_private_10_range(self):
        with pytest.raises(HTTPException):
            reject_if_private(ipaddress.ip_address("10.0.0.1"))

    def test_reject_if_private_rejects_private_172_range(self):
        with pytest.raises(HTTPException):
            reject_if_private(ipaddress.ip_address("172.16.0.1"))

    def test_reject_if_private_rejects_private_192_range(self):
        with pytest.raises(HTTPException):
            reject_if_private(ipaddress.ip_address("192.168.0.1"))

    def test_reject_if_private_rejects_loopback(self):
        with pytest.raises(HTTPException):
            reject_if_private(ipaddress.ip_address("127.0.0.1"))

    def test_reject_if_private_rejects_link_local(self):
        with pytest.raises(HTTPException):
            reject_if_private(ipaddress.ip_address("169.254.169.254"))

    def test_reject_if_private_rejects_ipv6_loopback(self):
        with pytest.raises(HTTPException):
            reject_if_private(ipaddress.ip_address("::1"))

    def test_reject_if_private_rejects_ipv6_link_local(self):
        with pytest.raises(HTTPException):
            reject_if_private(ipaddress.ip_address("fe80::1"))

    def test_reject_if_private_rejects_multicast(self):
        with pytest.raises(HTTPException):
            reject_if_private(ipaddress.ip_address("224.0.0.1"))

    def test_reject_if_private_rejects_unspecified(self):
        with pytest.raises(HTTPException):
            reject_if_private(ipaddress.ip_address("0.0.0.0"))

    def test_reject_if_private_accepts_public_ip(self):
        reject_if_private(ipaddress.ip_address("8.8.8.8"))

    def test_reject_if_private_accepts_public_ipv6(self):
        reject_if_private(ipaddress.ip_address("2001:4860:4860::8888"))


def _response(status: int, headers: dict[str, str] | None = None, method: str = "GET", url: str = "https://example.com/") -> httpx.Response:
    return httpx.Response(
        status_code=status,
        headers=headers or {},
        request=httpx.Request(method, url),
    )


def _make_client_returning(responses_by_url: dict[str, httpx.Response]) -> MagicMock:
    client = MagicMock(spec=httpx.AsyncClient)

    async def _dispatch(url, timeout, follow_redirects):
        return responses_by_url[url]

    client.get = AsyncMock(side_effect=_dispatch)
    client.head = AsyncMock(side_effect=_dispatch)
    return client


def _make_client_sequence(responses: list[httpx.Response]) -> MagicMock:
    client = MagicMock(spec=httpx.AsyncClient)
    it = iter(responses)

    async def _next(url, timeout, follow_redirects):
        return next(it)

    client.get = AsyncMock(side_effect=_next)
    client.head = AsyncMock(side_effect=_next)
    return client


@pytest.fixture
def public_dns(monkeypatch):
    async def fake_getaddrinfo(host, port, proto=0):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", port)),
        ]

    mock = MagicMock()
    mock.return_value.getaddrinfo = fake_getaddrinfo
    monkeypatch.setattr("asyncio.get_running_loop", mock)
    return mock


class TestResolveRedirects:
    @pytest.mark.asyncio
    async def test_resolve_redirects_returns_response_on_first_hop_for_non_redirect(self, public_dns):
        url = "https://example.com/thing"
        client = _make_client_returning({url: _response(200, {"content-type": "text/html"}, "GET", url)})

        resp, final_url = await resolve_redirects(client, "GET", url, timeout=30.0, max_redirects=5)

        assert resp.status_code == 200
        assert final_url == url

    @pytest.mark.asyncio
    async def test_resolve_redirects_follows_relative_location_via_urljoin(self, public_dns):
        start = "https://example.com/a/b"
        final = "https://example.com/c"
        client = _make_client_sequence([
            _response(302, {"location": "/c"}, "GET", start),
            _response(200, {}, "GET", final),
        ])

        resp, final_url = await resolve_redirects(client, "GET", start, timeout=30.0, max_redirects=5)

        assert resp.status_code == 200
        assert final_url == final

    @pytest.mark.asyncio
    async def test_resolve_redirects_follows_protocol_relative_location(self, public_dns):
        start = "https://example.com/x"
        client = _make_client_sequence([
            _response(302, {"location": "//other.example.com/y"}, "GET", start),
            _response(200, {}, "GET", "https://other.example.com/y"),
        ])

        resp, final_url = await resolve_redirects(client, "GET", start, timeout=30.0, max_redirects=5)

        assert resp.status_code == 200
        assert final_url == "https://other.example.com/y"

    @pytest.mark.asyncio
    async def test_resolve_redirects_rejects_missing_location_header(self, public_dns):
        start = "https://example.com/"
        client = _make_client_sequence([_response(302, {}, "GET", start)])

        with pytest.raises(HTTPException) as exc:
            await resolve_redirects(client, "GET", start, timeout=30.0, max_redirects=5)

        assert exc.value.status_code == 502
        assert "location" in exc.value.detail.lower()

    @pytest.mark.asyncio
    async def test_resolve_redirects_rejects_too_many_hops(self, public_dns):
        start = "https://example.com/"
        responses = [
            _response(302, {"location": f"/hop{i}"}, "GET", start)
            for i in range(10)
        ]
        client = _make_client_sequence(responses)

        with pytest.raises(HTTPException) as exc:
            await resolve_redirects(client, "GET", start, timeout=30.0, max_redirects=3)

        assert exc.value.status_code == 400
        assert "redirect" in exc.value.detail.lower()

    @pytest.mark.asyncio
    async def test_resolve_redirects_re_validates_each_hop_for_ssrf(self, public_dns):
        start = "https://example.com/"
        client = _make_client_sequence([
            _response(302, {"location": "https://169.254.169.254/latest/meta-data/"}, "GET", start),
        ])

        with pytest.raises(HTTPException) as exc:
            await resolve_redirects(client, "GET", start, timeout=30.0, max_redirects=5)

        assert exc.value.status_code == 400
        assert "blocked" in exc.value.detail.lower()

    @pytest.mark.asyncio
    async def test_resolve_redirects_respects_http_method_get(self, public_dns):
        url = "https://example.com/"
        client = _make_client_returning({url: _response(200, {}, "GET", url)})

        await resolve_redirects(client, "GET", url, timeout=30.0, max_redirects=5)

        assert client.get.await_count == 1
        assert client.head.await_count == 0

    @pytest.mark.asyncio
    async def test_resolve_redirects_respects_http_method_head(self, public_dns):
        url = "https://example.com/"
        client = _make_client_returning({url: _response(200, {}, "HEAD", url)})

        await resolve_redirects(client, "HEAD", url, timeout=10.0, max_redirects=5)

        assert client.head.await_count == 1
        assert client.get.await_count == 0
