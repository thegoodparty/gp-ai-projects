import asyncio
import base64
import json
import time

import httpx
import pytest

from broker.clerk_client import ClerkClient, ClerkClientError


def _make_client_with_backend(transport: httpx.MockTransport) -> ClerkClient:
    client = ClerkClient(
        secret_key="sk_test",
        frontend_api_base="https://x.clerk.app",
        agent_fleet_clerk_id="user_agent_fleet_test",
    )
    client._backend = httpx.AsyncClient(
        base_url="https://api.clerk.com",
        headers={"Authorization": "Bearer sk_test"},
        transport=transport,
    )
    return client


def _make_client_with_http(transport: httpx.MockTransport) -> ClerkClient:
    client = ClerkClient(
        secret_key="sk_test",
        frontend_api_base="https://x.clerk.app",
        agent_fleet_clerk_id="user_agent_fleet_test",
    )
    client._http = httpx.AsyncClient(transport=transport)
    return client


def _fake_jwt(exp_offset_seconds: int = 300) -> str:
    """Build a minimal JWT-shaped string with a real exp claim so
    ClerkClient._extract_exp() can decode it. Signature is meaningless;
    we never verify our own freshly-minted credential."""
    header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').rstrip(b"=").decode()
    payload = json.dumps({"exp": int(time.time()) + exp_offset_seconds}).encode()
    payload_b64 = base64.urlsafe_b64encode(payload).rstrip(b"=").decode()
    return f"{header}.{payload_b64}.sig"


class TestMintSessionJwt:
    async def test_returns_jwt_string_on_200(self):
        jwt_value = _fake_jwt()
        transport = httpx.MockTransport(
            lambda req: httpx.Response(200, json={"jwt": jwt_value})
        )
        client = _make_client_with_backend(transport)

        jwt = await client.mint_session_jwt("sess_123")

        assert jwt == jwt_value
        await client.aclose()

    async def test_raises_on_non_2xx(self):
        transport = httpx.MockTransport(
            lambda req: httpx.Response(404, json={"errors": [{"code": "session_not_found"}]})
        )
        client = _make_client_with_backend(transport)

        with pytest.raises(ClerkClientError, match="session JWT mint failed"):
            await client.mint_session_jwt("sess_missing")
        await client.aclose()

    async def test_raises_when_jwt_missing_in_response(self):
        transport = httpx.MockTransport(
            lambda req: httpx.Response(200, json={"object": "token"})
        )
        client = _make_client_with_backend(transport)

        with pytest.raises(ClerkClientError, match="missing 'jwt'"):
            await client.mint_session_jwt("sess_123")
        await client.aclose()

    async def test_sends_template_name_in_request_body(self):
        captured: dict = {}

        def handler(req: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(req.content)
            captured["path"] = req.url.path
            return httpx.Response(200, json={"jwt": _fake_jwt()})

        transport = httpx.MockTransport(handler)
        client = _make_client_with_backend(transport)

        await client.mint_session_jwt("sess_123")

        assert captured["body"] == {"template": "agent-mcp"}
        assert captured["path"] == "/v1/sessions/sess_123/tokens"
        await client.aclose()


class TestRedeemActorToken:
    async def test_posts_form_encoded_ticket_to_sign_ins_endpoint(self):
        captured: dict = {}

        def handler(req: httpx.Request) -> httpx.Response:
            captured["method"] = req.method
            captured["url"] = str(req.url)
            captured["content_type"] = req.headers.get("content-type")
            captured["body"] = req.content.decode()
            return httpx.Response(
                200,
                json={
                    "response": {
                        "status": "complete",
                        "created_session_id": "sess_abc",
                    }
                },
            )

        transport = httpx.MockTransport(handler)
        client = _make_client_with_http(transport)

        info = await client.redeem_actor_token(
            "https://x.clerk.app/v1/tickets/accept?ticket=jwt-value"
        )

        assert info == {"session_id": "sess_abc"}
        assert captured["method"] == "POST"
        assert captured["url"] == "https://x.clerk.app/v1/client/sign_ins"
        assert "application/x-www-form-urlencoded" in captured["content_type"]
        assert "strategy=ticket" in captured["body"]
        assert "ticket=jwt-value" in captured["body"]
        await client.aclose()

    async def test_raises_on_non_2xx(self):
        transport = httpx.MockTransport(
            lambda req: httpx.Response(
                400,
                json={
                    "errors": [{"code": "actor_token_already_used_code"}],
                },
            )
        )
        client = _make_client_with_http(transport)

        with pytest.raises(ClerkClientError, match="actor token redemption failed"):
            await client.redeem_actor_token(
                "https://x.clerk.app/v1/tickets/accept?ticket=jwt"
            )
        await client.aclose()

    async def test_raises_when_status_not_complete(self):
        transport = httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                json={"response": {"status": "needs_identifier"}},
            )
        )
        client = _make_client_with_http(transport)

        with pytest.raises(ClerkClientError, match="non-complete status"):
            await client.redeem_actor_token(
                "https://x.clerk.app/v1/tickets/accept?ticket=jwt"
            )
        await client.aclose()

    async def test_raises_when_created_session_id_missing(self):
        transport = httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                json={"response": {"status": "complete"}},
            )
        )
        client = _make_client_with_http(transport)

        with pytest.raises(ClerkClientError, match="missing created_session_id"):
            await client.redeem_actor_token(
                "https://x.clerk.app/v1/tickets/accept?ticket=jwt"
            )
        await client.aclose()

    async def test_raises_when_ticket_query_param_missing(self):
        client = _make_client_with_http(httpx.MockTransport(lambda req: httpx.Response(200)))
        with pytest.raises(ClerkClientError, match="missing ticket query parameter"):
            await client.redeem_actor_token("https://x.clerk.app/v1/tickets/accept")
        await client.aclose()

    async def test_rejects_url_outside_frontend_api_base(self):
        client = ClerkClient(
            secret_key="sk_test",
            frontend_api_base="https://expected.clerk.app",
            agent_fleet_clerk_id="user_agent_fleet_test",
        )
        with pytest.raises(ClerkClientError, match="not from the configured"):
            await client.redeem_actor_token(
                "https://attacker.com/v1/tickets/accept?ticket=xyz"
            )
        await client.aclose()


class TestCreateActorToken:
    async def test_returns_body_with_url(self):
        transport = httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                json={
                    "object": "actor_token",
                    "id": "act_xyz",
                    "url": "https://x.clerk.app/v1/client/sign_in_tokens/tok_1?token=jwt",
                    "token": "jwt-string",
                },
            )
        )
        client = _make_client_with_backend(transport)

        body = await client.create_actor_token("user_abc123")

        assert body["url"] == "https://x.clerk.app/v1/client/sign_in_tokens/tok_1?token=jwt"
        assert body["token"] == "jwt-string"
        await client.aclose()

    async def test_raises_on_non_2xx(self):
        transport = httpx.MockTransport(
            lambda req: httpx.Response(422, text='{"errors":[{"code":"user_not_found"}]}')
        )
        client = _make_client_with_backend(transport)

        with pytest.raises(ClerkClientError, match="actor token creation failed"):
            await client.create_actor_token("user_missing")
        await client.aclose()

    async def test_raises_when_url_missing_in_response(self):
        transport = httpx.MockTransport(
            lambda req: httpx.Response(200, json={"object": "actor_token", "id": "act_xyz"})
        )
        client = _make_client_with_backend(transport)

        with pytest.raises(ClerkClientError, match="missing 'url'"):
            await client.create_actor_token("user_abc123")
        await client.aclose()

    async def test_sends_correct_request_body_shape(self):
        captured: dict = {}

        def handler(req: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(req.content)
            captured["path"] = req.url.path
            return httpx.Response(
                200,
                json={"url": "https://x.clerk.app/v1/client/sign_in_tokens/tok_1?token=jwt"},
            )

        transport = httpx.MockTransport(handler)
        client = _make_client_with_backend(transport)

        await client.create_actor_token("user_abc123", expires_in_seconds=900)

        assert captured["path"] == "/v1/actor_tokens"
        assert captured["body"] == {
            "user_id": "user_abc123",
            "actor": {"sub": "user_agent_fleet_test"},
            "expires_in_seconds": 900,
        }
        await client.aclose()


class TestGetSessionJwt:
    """get_session_jwt is the cached entry point used by the proxy. It must mint
    once and reuse, but re-mint when the cached JWT is near expiry."""

    async def test_cache_miss_calls_clerk_and_returns_jwt(self):
        call_count = {"n": 0}
        jwt_value = _fake_jwt(exp_offset_seconds=300)

        def handler(req: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            return httpx.Response(200, json={"jwt": jwt_value})

        transport = httpx.MockTransport(handler)
        client = _make_client_with_backend(transport)

        result = await client.get_session_jwt("sess_abc")

        assert result == jwt_value
        assert call_count["n"] == 1
        await client.aclose()

    async def test_cache_hit_does_not_call_clerk_again(self):
        """Two sequential calls on the same session should hit Clerk exactly once."""
        call_count = {"n": 0}
        jwt_value = _fake_jwt(exp_offset_seconds=300)

        def handler(req: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            return httpx.Response(200, json={"jwt": jwt_value})

        transport = httpx.MockTransport(handler)
        client = _make_client_with_backend(transport)

        first = await client.get_session_jwt("sess_abc")
        second = await client.get_session_jwt("sess_abc")

        assert first == second == jwt_value
        assert call_count["n"] == 1
        await client.aclose()

    async def test_near_expiry_jwt_is_re_minted(self):
        """A cached JWT with exp = now + 3 should be considered stale and re-minted."""
        call_count = {"n": 0}
        # First mint returns a JWT that's near expiry; second mint returns fresh.
        responses = [
            _fake_jwt(exp_offset_seconds=3),
            _fake_jwt(exp_offset_seconds=300),
        ]

        def handler(req: httpx.Request) -> httpx.Response:
            jwt_value = responses[call_count["n"]]
            call_count["n"] += 1
            return httpx.Response(200, json={"jwt": jwt_value})

        transport = httpx.MockTransport(handler)
        client = _make_client_with_backend(transport)

        first = await client.get_session_jwt("sess_abc")
        second = await client.get_session_jwt("sess_abc")

        assert call_count["n"] == 2
        assert first != second
        await client.aclose()

    async def test_parallel_cache_miss_does_not_thundering_herd(self):
        """Concurrent get_session_jwt calls on the same session should produce
        exactly one Clerk mint, not N."""
        call_count = {"n": 0}
        jwt_value = _fake_jwt(exp_offset_seconds=300)

        async def handler(req: httpx.Request) -> httpx.Response:
            # Force interleaving so both tasks have a chance to enter the
            # function before either acquires the lock.
            await asyncio.sleep(0.01)
            call_count["n"] += 1
            return httpx.Response(200, json={"jwt": jwt_value})

        transport = httpx.MockTransport(handler)
        client = _make_client_with_backend(transport)

        results = await asyncio.gather(
            client.get_session_jwt("sess_abc"),
            client.get_session_jwt("sess_abc"),
            client.get_session_jwt("sess_abc"),
        )

        assert all(r == jwt_value for r in results)
        assert call_count["n"] == 1
        await client.aclose()

    async def test_different_sessions_are_cached_independently(self):
        call_count = {"n": 0}
        jwts = {
            "sess_a": _fake_jwt(exp_offset_seconds=300),
            "sess_b": _fake_jwt(exp_offset_seconds=300),
        }

        def handler(req: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            session_id = req.url.path.split("/")[3]
            return httpx.Response(200, json={"jwt": jwts[session_id]})

        transport = httpx.MockTransport(handler)
        client = _make_client_with_backend(transport)

        a1 = await client.get_session_jwt("sess_a")
        b1 = await client.get_session_jwt("sess_b")
        a2 = await client.get_session_jwt("sess_a")
        b2 = await client.get_session_jwt("sess_b")

        assert a1 == a2 == jwts["sess_a"]
        assert b1 == b2 == jwts["sess_b"]
        assert call_count["n"] == 2
        await client.aclose()
