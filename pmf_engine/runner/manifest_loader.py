"""Fetch a PMF experiment manifest + instruction from the broker.

The Fargate runner is fully quarantined and cannot reach S3 directly. Manifests
live in s3://agent-experiment-metadata-{env}/<id>/{manifest.json,instruction.md}
and are served to the runner via the broker's POST /experiment/manifest
endpoint, authenticated with the same X-Broker-Token (scope ticket) the runner
uses for everything else.
"""

from __future__ import annotations

import logging
from typing import NotRequired, TypedDict

import httpx

logger = logging.getLogger(__name__)


class ManifestEnvelope(TypedDict):
    """Envelope returned by POST /experiment/manifest.

    `manifest` and `instruction` are always present after envelope validation
    in `load_from_broker` — the previous `total=False` declaration was a
    correctness regression: subscript access like `envelope["manifest"]` was
    type-unsafe. Use `NotRequired` only for the truly optional fields the
    broker may omit on older versions.
    """

    manifest: dict
    instruction: str
    # Sidecar files keyed by basename. Empty dict (not absent) when the
    # experiment publishes no attachments — keeps downstream iteration
    # unconditional in the runner. NotRequired only because older brokers
    # (pre-attachment-feature) won't include the key at all.
    attachments: NotRequired[dict[str, str]]
    # VersionIds the broker actually resolved per attachment. Surfaced for
    # audit-trail symmetry with manifest+instruction pinning so operators can
    # cross-check what the runner saw against what dispatch HEADed at routing
    # time. Older brokers (or experiments with no attachments) omit this key.
    resolved_attachment_version_ids: NotRequired[dict[str, str]]


class ManifestLoadError(RuntimeError):
    """Raised when the broker fails to return a usable manifest envelope."""


# Mirror of pmf_engine.control_plane.manifest_loader._PERMISSION_MODE_VALUES.
# Kept inline (not imported) so the runner-side loader has no control-plane
# import dependency; keep the two in sync if either side changes.
_PERMISSION_MODE_VALUES = frozenset({"default", "bypassPermissions"})


def _validate_write_action_fields(manifest: dict) -> None:
    """Defense-in-depth validation for the optional write-action manifest
    fields (system_prompt, permission_mode, allowed_external_tools).

    These are validated at publish time (meta-schema) and again at dispatch
    time (control_plane.manifest_loader._validate_write_action_fields — the
    authoritative ruleset). This runner-side mirror catches hand-edited
    manifests used in local-dev / debug runs that bypass dispatch.
    """
    permission_mode = manifest.get("permission_mode")
    if permission_mode is not None:
        if not isinstance(permission_mode, str) or permission_mode not in _PERMISSION_MODE_VALUES:
            raise ManifestLoadError(
                f"manifest.permission_mode must be one of {sorted(_PERMISSION_MODE_VALUES)}; "
                f"got {permission_mode!r}"
            )

    system_prompt = manifest.get("system_prompt")
    if system_prompt is not None:
        if not isinstance(system_prompt, str) or not system_prompt.strip():
            raise ManifestLoadError("manifest.system_prompt must be a non-empty string")

    tools = manifest.get("allowed_external_tools")
    if tools is not None:
        if not isinstance(tools, list):
            raise ManifestLoadError("manifest.allowed_external_tools must be a list")
        for t in tools:
            if not isinstance(t, str) or not t.strip():
                raise ManifestLoadError(
                    f"manifest.allowed_external_tools entry must be a non-empty string; got {t!r}"
                )

    # runtime.max_parallel_subagents — opt-in for parallel research fan-out.
    # Optional nested block; when present it must be a dict and the field, if
    # set, a non-negative int (bool excluded — it's an int subclass in Python
    # but a boolean here is an authoring mistake). The harness clamps the value
    # to its own ceiling; this only rejects ill-typed/negative input.
    runtime = manifest.get("runtime")
    if runtime is not None:
        if not isinstance(runtime, dict):
            raise ManifestLoadError(f"manifest.runtime must be an object; got {runtime!r}")
        mps = runtime.get("max_parallel_subagents")
        if mps is not None and (
            isinstance(mps, bool) or not isinstance(mps, int) or mps < 0
        ):
            raise ManifestLoadError(
                f"manifest.runtime.max_parallel_subagents must be a non-negative integer; got {mps!r}"
            )


def load_from_broker(
    experiment_id: str,
    broker_url: str,
    broker_token: str,
    manifest_version_id: str | None = None,
    instruction_version_id: str | None = None,
    attachment_version_ids: dict[str, str] | None = None,
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
    if attachment_version_ids:
        request_body["attachment_version_ids"] = attachment_version_ids

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

        # Write-action fields (ENG-10128) are optional but, when present, must
        # be well-formed before main.py reads them onto RunnerConfig and the
        # harness wires them into ClaudeAgentOptions.
        _validate_write_action_fields(manifest)

        # Attachments are optional — older broker versions that haven't been
        # redeployed yet don't include the field. Distinguish "key absent"
        # (broker too old to ship attachments — emit operator-facing INFO so
        # the discrepancy is visible) from "key present but {}" (broker is
        # current, this experiment publishes no attachments — quiet).
        has_attachments_key = "attachments" in envelope
        if not has_attachments_key:
            logger.info(
                "broker did not return attachments key — running against older broker? "
                "experiment_id=%s",
                experiment_id,
            )
        raw_attachments = envelope.get("attachments", {})
        if raw_attachments is None:
            raw_attachments = {}
        if not isinstance(raw_attachments, dict):
            raise ManifestLoadError("envelope.attachments must be an object (basename → body)")
        attachments: dict[str, str] = {}
        for name, body in raw_attachments.items():
            if not isinstance(name, str) or not isinstance(body, str):
                raise ManifestLoadError(
                    f"envelope.attachments['{name}'] must map a string basename to a string body"
                )
            attachments[name] = body

        # Resolved attachment VersionIds — broker's audit-trail echo of what
        # it actually fetched after the pinning request body was applied.
        # Optional and only surfaced when present; legacy brokers (or
        # experiments with no attachments) omit it entirely.
        raw_resolved = envelope.get("resolved_attachment_version_ids")
        resolved_attachment_version_ids: dict[str, str] | None = None
        if raw_resolved is not None:
            if not isinstance(raw_resolved, dict):
                raise ManifestLoadError(
                    "envelope.resolved_attachment_version_ids must be an object "
                    "(basename → version_id)"
                )
            resolved_attachment_version_ids = {}
            for name, vid in raw_resolved.items():
                if not isinstance(name, str) or not isinstance(vid, str):
                    raise ManifestLoadError(
                        f"envelope.resolved_attachment_version_ids['{name}'] must map "
                        f"a string basename to a string version_id"
                    )
                resolved_attachment_version_ids[name] = vid

        result: ManifestEnvelope = {
            "manifest": manifest,
            "instruction": instruction,
            "attachments": attachments,
        }
        if resolved_attachment_version_ids is not None:
            result["resolved_attachment_version_ids"] = resolved_attachment_version_ids
        return result
    finally:
        if owns_client and client is not None:
            client.close()
