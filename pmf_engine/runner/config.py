from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from urllib.parse import urlparse, urlunparse

from shared.logger import get_logger

logger = get_logger(__name__)

_AWS_DEPLOYMENT_ENVS = ("dev", "qa", "prod")


class BrokerUrlSchemeError(ValueError):
    """Raised when BROKER_URL fails the https-only guard in AWS deployment envs.

    Distinct subclass so `type(e).__name__` in CloudWatch alarm / telemetry
    disambiguates this from other config-parse ValueErrors.
    """


def _redact_userinfo(url: str) -> str:
    try:
        parsed = urlparse(url)
    except ValueError:
        return "<url-redacted>"
    if parsed.hostname is None:
        if "@" in parsed.netloc:
            netloc = parsed.netloc.rsplit("@", 1)[1]
            return urlunparse(parsed._replace(netloc=netloc))
        return "<url-redacted>"
    host = parsed.hostname
    if ":" in host:
        host = f"[{host}]"
    netloc = host
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    return urlunparse(parsed._replace(netloc=netloc))


_COMBINATOR_KEYS = ("oneOf", "anyOf", "allOf")
_MAX_COMBINATOR_DEPTH = 4


def _is_draft7_object_schema(schema: object, _depth: int = 0) -> bool:
    """Quick shape check: is this a real Draft-07 schema, not the legacy GP shape?

    Accepts:
      - `{"type": "object", "properties": {...}}` — the common single-shape form.
      - `{"oneOf": [...]}` / `{"anyOf": [...]}` / `{"allOf": [...]}` where every
        branch itself satisfies this check — combinator-at-root schemas used to
        discriminate between artifact variants (e.g. status-keyed shapes in
        meeting_briefing and meeting_schedule). The recursion makes
        `{"oneOf": [{}]}` (and other no-op-branch variants) get rejected the
        same way `{"oneOf": []}` is — a branch of `{}` would let Draft7Validator
        accept every artifact.

    Rejects:
      - The legacy GP-shape `{"name": "string", ...}` example-dict format, which
        Draft7Validator treats as a no-op (every artifact validates).
      - Empty combinators like `{"oneOf": []}` and no-op-branch combinators like
        `{"oneOf": [{}]}` — structurally a combinator but declares no
        constraints, so it's equivalent to the legacy form.

    Defensive depth limit (4) guards against pathological nesting; real
    manifests stay well under that.
    """
    if _depth > _MAX_COMBINATOR_DEPTH:
        return False
    if not isinstance(schema, dict):
        return False
    if schema.get("type") == "object" and isinstance(schema.get("properties"), dict):
        return True
    for combinator in _COMBINATOR_KEYS:
        branches = schema.get(combinator)
        if isinstance(branches, list) and branches:
            if all(_is_draft7_object_schema(b, _depth + 1) for b in branches):
                return True
    return False


def validate_broker_url_scheme(broker_url: str, environment: str) -> None:
    """Raise BrokerUrlSchemeError if broker_url scheme is plaintext in a deployment env.

    Standalone helper so the runner entrypoint can guard scheme BEFORE calling
    init_config() — a misconfigured plaintext BROKER_URL must never be wired
    into the broker client even if the failed-callback later runs over it.
    """
    env_normalized = environment.strip().lower()
    if not broker_url:
        if env_normalized in _AWS_DEPLOYMENT_ENVS:
            raise BrokerUrlSchemeError(
                f"BROKER_URL must be set in environment={env_normalized!r}. "
                f"A runner in a deployed environment cannot operate without the broker. "
                f"Set BROKER_URL on the ECS task definition "
                f"(infrastructure/modules/pmf-engine-fargate) to https://broker-{env_normalized}.ai.goodparty.org."
            )
        return
    scheme = broker_url.split("://", 1)[0].lower() if "://" in broker_url else ""
    if env_normalized in _AWS_DEPLOYMENT_ENVS and scheme != "https":
        safe = _redact_userinfo(broker_url)
        raise BrokerUrlSchemeError(
            f"BROKER_URL must use https:// in environment={env_normalized!r}; "
            f"got scheme={scheme!r} url={safe!r}. Plaintext http:// is only "
            f"permitted outside {list(_AWS_DEPLOYMENT_ENVS)} (for local in-process broker). "
            f"Set BROKER_URL on the ECS task definition "
            f"(infrastructure/modules/pmf-engine-fargate) to a URL beginning with https://."
        )


@dataclass
class RunnerConfig:
    experiment_id: str
    run_id: str
    organization_slug: str
    instruction: str
    params: dict = field(default_factory=dict)
    harness: str = "claude_sdk"
    model: str = "sonnet"
    environment: str = "dev"
    broker_url: str = ""
    broker_token: str = ""
    contract_schema: dict | None = None
    max_turns: int = 50
    timeout_seconds: int = 600
    # Sidecar files the broker shipped alongside instruction.md. Basename →
    # UTF-8 body; the runner writes each one to /workspace/<basename> before
    # spawning the agent. Default-empty so legacy code paths (INSTRUCTION env
    # override for local-dev runs) work without explicit plumbing.
    attachments: dict[str, str] = field(default_factory=dict)
    # Write-action manifest fields (ENG-10128). All optional — when absent the
    # harness falls back to its legacy defaults (capability-prompt only,
    # bypassPermissions, ALLOWED_TOOLS only). The runner-side manifest loader
    # validates these before they reach here.
    system_prompt: str | None = None
    permission_mode: str | None = None
    allowed_external_tools: list[str] | None = None
    # Parallel research fan-out opt-in (manifest.runtime.max_parallel_subagents).
    # 0 = disabled (default); the harness clamps to its own ceiling.
    max_parallel_subagents: int = 0

    @classmethod
    def from_env(cls) -> RunnerConfig:
        params_raw = os.environ.get("PARAMS_JSON", "{}")
        try:
            params = json.loads(params_raw)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError(f"Invalid PARAMS_JSON: {exc}") from exc

        if params is None:
            params = {}
        elif not isinstance(params, dict):
            raise ValueError(
                f"PARAMS_JSON must decode to an object, got {type(params).__name__}"
            )

        experiment_id = os.environ.get("EXPERIMENT_ID", "")
        instruction = os.environ.get("INSTRUCTION", "")
        # `harness` field was dropped from the manifest (only value was
        # claude_sdk). Hardcode here; if multiple harnesses ever land,
        # plumb a HARNESS env back through.
        harness = "claude_sdk"
        model = os.environ.get("AGENT_MODEL", "sonnet")
        max_turns = 50
        timeout_seconds = 600

        environment_raw = os.environ.get("ENVIRONMENT", "dev")
        environment_normalized = environment_raw.strip().lower()
        broker_url_raw = os.environ.get("BROKER_URL", "")
        broker_url = broker_url_raw.strip()

        # Validate the BROKER_URL scheme BEFORE any broker fetch — otherwise a
        # plaintext http:// in a deployment env would leak the broker token +
        # manifest body in cleartext between here and the validation below.
        # main.py already calls this earlier in the entrypoint; the second
        # call here is defense-in-depth for any code path that constructs
        # RunnerConfig directly (tests, future CLI, etc.).
        validate_broker_url_scheme(broker_url, environment_raw)

        contract_schema = None
        attachments: dict[str, str] = {}
        system_prompt: str | None = None
        permission_mode: str | None = None
        allowed_external_tools: list[str] | None = None
        max_parallel_subagents: int = 0
        if experiment_id:
            # The broker is the only source for manifest+instruction. The
            # broker reads s3://agent-experiment-metadata-{env}/<id>/* and
            # returns {manifest, instruction}. Failures must be loud — there
            # is no bundled fallback. INSTRUCTION env var (if present) is
            # ignored to prevent stale-env footguns.
            broker_url_for_manifest = os.environ.get("BROKER_URL", "").strip()
            broker_token_for_manifest = os.environ.get("BROKER_TOKEN", "").strip()
            if not (broker_url_for_manifest and broker_token_for_manifest):
                raise RuntimeError(
                    "Cannot resolve experiment manifest: "
                    "BROKER_URL and BROKER_TOKEN must both be set. "
                    "Local-dev runs must point at scripts/local_runtime.py."
                )
            # ATTACHMENT_VERSION_IDS is the per-attachment pinning dict the
            # dispatch Lambda serialized (sort_keys=True for byte-determinism).
            # Parsing/validating here means a malformed env value raises early
            # — before the broker call — rather than producing a confusing
            # broker-side rejection. Empty/whitespace string is treated the
            # same as unset, mirroring the MANIFEST_VERSION_ID convention.
            attachment_version_ids_raw = os.environ.get("ATTACHMENT_VERSION_IDS", "").strip()
            attachment_version_ids: dict[str, str] | None = None
            if attachment_version_ids_raw:
                try:
                    parsed_avi = json.loads(attachment_version_ids_raw)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"Invalid ATTACHMENT_VERSION_IDS: {exc}"
                    ) from exc
                if not isinstance(parsed_avi, dict):
                    raise ValueError(
                        f"ATTACHMENT_VERSION_IDS must decode to an object, "
                        f"got {type(parsed_avi).__name__}"
                    )
                # Defend against env-var tampering and narrow types for the
                # type checker — dispatch is trusted but we cross a process
                # boundary so this is cheap insurance.
                attachment_version_ids = {str(k): str(v) for k, v in parsed_avi.items()}

            from pmf_engine.runner.manifest_loader import load_from_broker
            envelope = load_from_broker(
                experiment_id=experiment_id,
                broker_url=broker_url_for_manifest,
                broker_token=broker_token_for_manifest,
                manifest_version_id=os.environ.get("MANIFEST_VERSION_ID", "").strip() or None,
                instruction_version_id=os.environ.get("INSTRUCTION_VERSION_ID", "").strip() or None,
                attachment_version_ids=attachment_version_ids,
            )
            manifest = envelope["manifest"]
            instruction = envelope["instruction"]
            attachments = dict(envelope.get("attachments") or {})
            model = manifest.get("model", model)
            max_turns = manifest.get("max_turns", max_turns)
            timeout_seconds = manifest.get("timeout_seconds", timeout_seconds)
            contract_schema = manifest.get("output_schema")
            if contract_schema is not None and not _is_draft7_object_schema(contract_schema):
                raise ValueError(
                    f"Manifest output_schema must be a JSON Schema Draft-07 object: "
                    f"either type='object' with a properties dict, or a non-empty "
                    f"oneOf/anyOf/allOf with at least one well-formed branch; "
                    f"got {contract_schema!r}"
                )
            # Write-action fields (ENG-10128) flow through the manifest body.
            # Already type-validated in manifest_loader._validate_write_action_fields;
            # here we just project them onto RunnerConfig so the harness can read
            # them as ordinary attributes.
            system_prompt = manifest.get("system_prompt")
            permission_mode = manifest.get("permission_mode")
            allowed_external_tools = manifest.get("allowed_external_tools")
            # Nested fan-out opt-in. Validated in manifest_loader; project the
            # int onto the flat RunnerConfig field the harness reads (0 if the
            # runtime block or field is absent).
            runtime_block = manifest.get("runtime") or {}
            max_parallel_subagents = runtime_block.get("max_parallel_subagents", 0) or 0

        ts_raw = os.environ.get("TIMEOUT_SECONDS", "").strip()
        if ts_raw:
            try:
                timeout_seconds = int(ts_raw)
            except ValueError:
                raise ValueError(
                    f"TIMEOUT_SECONDS must be an integer; got {ts_raw!r}"
                )

        return cls(
            experiment_id=experiment_id,
            run_id=os.environ.get("RUN_ID", ""),
            organization_slug=os.environ.get("ORGANIZATION_SLUG", ""),
            instruction=instruction,
            params=params,
            harness=harness,
            model=model,
            environment=environment_normalized,
            broker_url=broker_url,
            broker_token=os.environ.get("BROKER_TOKEN", ""),
            contract_schema=contract_schema,
            max_turns=max_turns,
            timeout_seconds=timeout_seconds,
            attachments=attachments,
            system_prompt=system_prompt,
            permission_mode=permission_mode,
            allowed_external_tools=allowed_external_tools,
            max_parallel_subagents=max_parallel_subagents,
        )
