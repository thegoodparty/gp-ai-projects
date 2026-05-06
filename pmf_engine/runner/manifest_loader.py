"""Fetch a PMF experiment manifest + instruction from the broker.

The Fargate runner is fully quarantined and cannot reach S3 directly. Manifests
live in s3://agent-experiment-metadata-{env}/<id>/{manifest.json,instruction.md}
and are served to the runner via the broker's POST /experiment/manifest
endpoint, authenticated with the same X-Broker-Token (scope ticket) the runner
uses for everything else.
"""

from __future__ import annotations

import logging
from typing import TypedDict

import httpx

logger = logging.getLogger(__name__)


class ManifestEnvelope(TypedDict):
    manifest: dict
    instruction: str


class ManifestLoadError(RuntimeError):
    """Raised when the broker fails to return a usable manifest envelope."""


def load_from_broker(
    experiment_id: str,
    broker_url: str,
    broker_token: str,
    manifest_version_id: str | None = None,
    instruction_version_id: str | None = None,
    timeout_seconds: float = 30.0,
    client: httpx.Client | None = None,
) -> ManifestEnvelope:
    """POST /experiment/manifest and validate the envelope shape.

    `manifest_version_id` / `instruction_version_id` pin the broker's S3 fetch
    to specific object versions (captured by the dispatch Lambda). When set,
    the runner reads the exact bytes Lambda saw at routing time, even if a
    publish has happened in between. Both omitted = "latest" (dev/local only).

    A passed `client` overrides the default — useful for tests + for sharing
    the runner's pre-configured httpx.Client with base_url + auth headers.
    """
    if not experiment_id:
        raise ManifestLoadError("experiment_id required")
    if not broker_url:
        raise ManifestLoadError("broker_url required")
    if not broker_token:
        raise ManifestLoadError("broker_token required")

    owns_client = client is None
    if owns_client:
        client = httpx.Client(
            base_url=broker_url,
            headers={"X-Broker-Token": broker_token},
            timeout=timeout_seconds,
        )

    request_body: dict = {"experiment_id": experiment_id}
    if manifest_version_id:
        request_body["manifest_version_id"] = manifest_version_id
    if instruction_version_id:
        request_body["instruction_version_id"] = instruction_version_id

    try:
        try:
            response = client.post("/experiment/manifest", json=request_body)
        except httpx.HTTPError as e:
            logger.error("manifest fetch transport error experiment_id=%s: %s", experiment_id, e)
            raise ManifestLoadError(f"broker transport error: {type(e).__name__}") from e

        if response.status_code != 200:
            detail = ""
            try:
                detail = response.json().get("detail", "")
            except Exception:
                detail = response.text[:200]
            logger.error(
                "manifest fetch non-200 experiment_id=%s status=%s detail=%s",
                experiment_id, response.status_code, detail,
            )
            raise ManifestLoadError(
                f"broker returned {response.status_code} for experiment '{experiment_id}': {detail}"
            )

        try:
            envelope = response.json()
        except Exception as e:
            raise ManifestLoadError(f"broker returned non-JSON body: {e}") from e

        manifest = envelope.get("manifest")
        instruction = envelope.get("instruction")
        if not isinstance(manifest, dict):
            raise ManifestLoadError("envelope.manifest missing or not an object")
        if not isinstance(instruction, str) or not instruction.strip():
            raise ManifestLoadError("envelope.instruction missing or empty")

        # Sanity check the manifest has the runner-critical fields. Catches
        # accidental schema drift (e.g., publishing the wrong file shape) at
        # the runner boundary instead of crashing later inside the harness.
        for required in ("output_schema", "model", "max_turns"):
            if required not in manifest:
                raise ManifestLoadError(f"manifest missing required field '{required}'")

        return ManifestEnvelope(manifest=manifest, instruction=instruction)
    finally:
        if owns_client and client is not None:
            client.close()
