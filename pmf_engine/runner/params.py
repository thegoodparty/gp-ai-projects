"""Fetch large experiment params from the broker.

Small params ride the PARAMS_JSON env var inline. When they exceed the ECS
RunTask containerOverrides budget, the dispatch omits PARAMS_JSON and sets
PARAMS_VIA_BROKER=1; the runner then pulls params from the broker's
``/params/read`` endpoint, which returns them from this run's scope ticket
(minted with the full params at launch). Mirrors input_files.py's broker-client
usage — the broker's long-lived task role is the egress gate, so no AWS
credentials live on the runner.
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)


def fetch_params_from_broker(
    *,
    broker_url: str,
    broker_token: str,
    client: httpx.Client | None = None,
) -> dict:
    """Fetch this run's params from the broker. Returns the params dict.

    Failures bubble up: params are required to run the agent, so a fetch failure
    means the run is doomed — fail fast so main()'s config-load handler reports
    FAILED to gp-api cleanly. A `client` arg is accepted for tests
    (httpx.MockTransport); when omitted, the helper owns the httpx.Client.
    """
    owns_client = client is None
    if owns_client:
        client = httpx.Client(
            base_url=broker_url,
            headers={"X-Broker-Token": broker_token},
            timeout=30.0,
        )
    try:
        response = client.get("/params/read")
        response.raise_for_status()
        params = response.json()
    finally:
        if owns_client:
            client.close()

    if not isinstance(params, dict):
        raise ValueError(
            f"/params/read must return an object, got {type(params).__name__}"
        )
    logger.info("fetched params from broker keys=%d", len(params))
    return params
