from __future__ import annotations

import httpx


class BrokerError(Exception):
    def __init__(self, status_code: int, detail: str, user_safe_message: str = ""):
        self.status_code = status_code
        self.detail = detail
        self.user_safe_message = user_safe_message
        super().__init__(detail)


class BrokerClient:
    def __init__(self, broker_url: str, service_token: str):
        self.broker_url = broker_url.rstrip("/")
        self.service_token = service_token

    def mint_run_token(
        self,
        run_id: str,
        organization_slug: str,
        experiment_id: str,
        scope: dict,
        params: dict,
        exp_ttl_seconds: int = 3600,
        prior_artifact_versions: dict[str, str] | None = None,
    ) -> dict:
        body = {
            "run_id": run_id,
            "organization_slug": organization_slug,
            "experiment_id": experiment_id,
            "scope": scope,
            "params": params,
            "exp_ttl_seconds": exp_ttl_seconds,
            "prior_artifact_versions": prior_artifact_versions,
        }
        response = httpx.post(
            f"{self.broker_url}/internal/mint-run-token",
            json=body,
            headers={"Authorization": f"Bearer {self.service_token}"},
            timeout=30.0,
        )
        if response.status_code == 401:
            raise BrokerError(401, "Invalid service token")
        if response.status_code == 400:
            data = response.json()
            raise BrokerError(400, data.get("detail", ""), data.get("user_safe_message", ""))
        if response.status_code == 409:
            raise BrokerError(409, "Duplicate run_id")
        response.raise_for_status()
        return response.json()

    def delete_run_token(self, broker_token: str, run_id: str) -> None:
        response = httpx.post(
            f"{self.broker_url}/internal/delete-run-token",
            json={"broker_token": broker_token, "run_id": run_id},
            headers={"Authorization": f"Bearer {self.service_token}"},
            timeout=10.0,
        )
        if response.status_code in (200, 204):
            return
        if response.status_code == 401:
            raise BrokerError(401, "Invalid service token")
        response.raise_for_status()
