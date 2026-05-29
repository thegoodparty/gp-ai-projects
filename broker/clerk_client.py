import httpx


class ClerkClientError(Exception):
    pass


class ClerkClient:
    def __init__(
        self,
        secret_key: str,
        frontend_api_base: str,
        agent_fleet_clerk_id: str,
    ):
        self._backend = httpx.AsyncClient(
            base_url="https://api.clerk.com",
            headers={"Authorization": f"Bearer {secret_key}"},
            timeout=15,
        )

    async def aclose(self) -> None:
        await self._backend.aclose()
