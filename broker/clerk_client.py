import asyncio
import base64
import json
import time
from typing import TypedDict
from urllib.parse import parse_qs, urlsplit

import httpx


class ClerkSessionInfo(TypedDict):
    session_id: str


class ClerkClientError(Exception):
    pass


class ClerkClient:
    def __init__(
        self,
        secret_key: str,
        frontend_api_base: str,
        agent_fleet_clerk_id: str,
    ):
        self._secret_key = secret_key
        self._frontend_api_base = frontend_api_base.rstrip("/")
        self._agent_fleet_clerk_id = agent_fleet_clerk_id
        self._http = httpx.AsyncClient(timeout=15)
        self._backend = httpx.AsyncClient(
            base_url="https://api.clerk.com",
            headers={"Authorization": f"Bearer {secret_key}"},
            timeout=15,
        )
        self._jwt_cache: dict[str, tuple[str, int]] = {}
        self._cache_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._http.aclose()
        await self._backend.aclose()

    async def create_actor_token(
        self, user_id: str, expires_in_seconds: int = 600
    ) -> dict:
        """POST /v1/actor_tokens — mints a one-shot sign-in URL stamped with the
        agent fleet as actor. The broker redeems this URL immediately afterward
        via redeem_actor_token() to produce a real Clerk session."""
        resp = await self._backend.post(
            "/v1/actor_tokens",
            json={
                "user_id": user_id,
                "actor": {"sub": self._agent_fleet_clerk_id},
                "expires_in_seconds": expires_in_seconds,
            },
        )
        if resp.status_code >= 400:
            raise ClerkClientError(
                f"actor token creation failed "
                f"status={resp.status_code} body={resp.text[:500]}"
            )
        body = resp.json()
        url = body.get("url")
        if not url:
            raise ClerkClientError(
                f"actor token creation response missing 'url'; keys={list(body.keys())}"
            )
        return body

    async def redeem_actor_token(self, actor_token_url: str) -> ClerkSessionInfo:
        # actor_token_url comes back from POST /v1/actor_tokens as
        # https://<frontend-api>/v1/tickets/accept?ticket=<jwt>. That URL is the
        # browser-facing Account Portal flow (POST→405, GET→HTML), not a
        # server-redeemable JSON endpoint. Extract the ticket and submit it to
        # POST /v1/client/sign_ins with strategy=ticket, which is what
        # @clerk/clerk-js does internally via signIn.ticket({ ticket }). Response
        # is JSON: { response: { status: "complete", created_session_id, ... } }.
        if not actor_token_url.startswith(f"{self._frontend_api_base}/"):
            raise ClerkClientError(
                f"actor token URL is not from the configured Clerk frontend API base "
                f"(base={self._frontend_api_base}, got={actor_token_url[:64]}...)"
            )
        ticket_values = parse_qs(urlsplit(actor_token_url).query).get("ticket")
        if not ticket_values:
            raise ClerkClientError(
                f"actor token URL missing ticket query parameter (url={actor_token_url[:80]}...)"
            )
        resp = await self._http.post(
            f"{self._frontend_api_base}/v1/client/sign_ins",
            data={"strategy": "ticket", "ticket": ticket_values[0]},
        )
        if resp.status_code >= 400:
            raise ClerkClientError(
                f"actor token redemption failed status={resp.status_code} body={resp.text[:500]}"
            )
        body = resp.json()
        response = body.get("response") or {}
        status = response.get("status")
        if status != "complete":
            raise ClerkClientError(
                f"actor token redemption returned non-complete status={status} "
                f"errors={body.get('errors')}"
            )
        session_id = response.get("created_session_id")
        if not session_id:
            raise ClerkClientError(
                f"actor token redemption response missing created_session_id; "
                f"response_keys={list(response.keys())}"
            )
        return {"session_id": session_id}

    async def mint_session_jwt(
        self, session_id: str, template: str = "agent-mcp"
    ) -> str:
        resp = await self._backend.post(
            f"/v1/sessions/{session_id}/tokens",
            json={"template": template},
        )
        if resp.status_code >= 400:
            raise ClerkClientError(
                f"session JWT mint failed status={resp.status_code} body={resp.text[:500]}"
            )
        jwt_value = resp.json().get("jwt")
        if not jwt_value:
            raise ClerkClientError("session JWT mint response missing 'jwt'")
        return jwt_value

    async def get_session_jwt(self, session_id: str) -> str:
        """Cached: returns a JWT for the session, minting if absent or near expiry.
        One Clerk mint per session per ~4.5 minutes regardless of request volume."""
        now = int(time.time())
        cached = self._jwt_cache.get(session_id)
        if cached and cached[1] - now > 5:
            return cached[0]
        async with self._cache_lock:
            cached = self._jwt_cache.get(session_id)
            if cached and cached[1] - now > 5:
                return cached[0]
            jwt_value = await self.mint_session_jwt(session_id)
            exp = self._extract_exp(jwt_value)
            self._jwt_cache[session_id] = (jwt_value, exp)
            return jwt_value

    @staticmethod
    def _extract_exp(jwt_value: str) -> int:
        """Read the exp claim from a JWT without verifying (we just minted it,
        don't need to re-verify our own credential)."""
        parts = jwt_value.split(".")
        if len(parts) != 3:
            raise ClerkClientError("invalid JWT structure")
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        exp = payload.get("exp")
        if not isinstance(exp, int):
            raise ClerkClientError("JWT missing or non-numeric exp claim")
        return exp
