import asyncio
import time

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from broker.dynamodb_client import ScopeTicket
from broker.endpoints.http_fetch import (
    get_http_client,
    get_scope_ticket,
    router,
)


class _SyncASGIBridge(httpx.BaseTransport):
    def __init__(self, app: FastAPI):
        self._async = httpx.ASGITransport(app=app)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        async def _run() -> httpx.Response:
            resp = await self._async.handle_async_request(request)
            await resp.aread()
            return resp

        resp = asyncio.run(_run())
        return httpx.Response(
            status_code=resp.status_code,
            headers=resp.headers,
            content=resp.content,
            request=request,
        )


def _make_ticket() -> ScopeTicket:
    now = int(time.time())
    return ScopeTicket(
        pk="broker-token-head-contract",
        run_id="run-head-contract-001",
        organization_slug="org-1",
        experiment_id="meeting_briefing",
        scope={},
        params={},
        exp=now + 3600,
        issued_at=now,
        issued_by="dispatch-lambda-dev",
    )


def _broker_app(upstream, monkeypatch) -> FastAPI:
    async def _no_ssrf(url: str) -> None:
        return None

    monkeypatch.setattr("broker.ssrf_guard.validate_url", _no_ssrf)
    monkeypatch.setattr("broker.endpoints.http_fetch.validate_url", _no_ssrf)

    app = FastAPI()
    app.include_router(router)
    upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    app.dependency_overrides[get_scope_ticket] = _make_ticket
    app.dependency_overrides[get_http_client] = lambda: upstream_client
    return app


def _bind_real_client(app: FastAPI):
    from pmf_engine.runner.pmf_runtime.config import init_config

    transport = _SyncASGIBridge(app)
    client = httpx.Client(transport=transport, base_url="http://broker")
    cfg = init_config("http://broker", "tok")
    cfg._client = client
    return cfg


def _reset_runtime_config():
    import pmf_engine.runner.pmf_runtime.config as config_mod

    config_mod._config = None


class TestHeadContract:
    def setup_method(self):
        _reset_runtime_config()

    def test_live_url_round_trips_status_and_final_url(self, monkeypatch):
        def upstream(req: httpx.Request) -> httpx.Response:
            assert req.method == "HEAD"
            return httpx.Response(200)

        app = _broker_app(upstream, monkeypatch)
        _bind_real_client(app)

        from pmf_engine.runner.pmf_runtime.http import head

        result = head("https://example.gov/p")
        assert result["status"] == 200
        assert result["final_url"] == "https://example.gov/p"

    def test_redirect_final_url_is_the_redirect_target(self, monkeypatch):
        def upstream(req: httpx.Request) -> httpx.Response:
            if req.url.path == "/old":
                return httpx.Response(301, headers={"location": "https://example.gov/new"})
            return httpx.Response(200)

        app = _broker_app(upstream, monkeypatch)
        _bind_real_client(app)

        from pmf_engine.runner.pmf_runtime.http import head

        result = head("https://example.gov/old")
        assert result["status"] == 200
        assert result["final_url"] == "https://example.gov/new"

    def test_broker_error_envelope_surfaces_as_value_error_with_detail(self, monkeypatch):
        async def _block(url: str) -> None:
            raise HTTPException(status_code=400, detail="SSRF blocked")

        monkeypatch.setattr("broker.ssrf_guard.validate_url", _block)
        monkeypatch.setattr("broker.endpoints.http_fetch.validate_url", _block)

        def upstream(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200)

        app = FastAPI()
        app.include_router(router)
        upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        app.dependency_overrides[get_scope_ticket] = _make_ticket
        app.dependency_overrides[get_http_client] = lambda: upstream_client

        _bind_real_client(app)

        from pmf_engine.runner.pmf_runtime.http import head

        with pytest.raises(ValueError, match="SSRF blocked"):
            head("http://10.0.0.5/internal")
