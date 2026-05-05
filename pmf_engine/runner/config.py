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
        return url
    if parsed.hostname is None:
        if "@" in parsed.netloc:
            netloc = parsed.netloc.rsplit("@", 1)[1]
            return urlunparse(parsed._replace(netloc=netloc))
        return url
    host = parsed.hostname
    if ":" in host:
        host = f"[{host}]"
    netloc = host
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    return urlunparse(parsed._replace(netloc=netloc))


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
    contract_constraints: dict | None = None
    max_turns: int = 50
    timeout_seconds: int = 600

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
        harness = os.environ.get("HARNESS", "claude_sdk")
        model = os.environ.get("AGENT_MODEL", "sonnet")
        max_turns = 50
        timeout_seconds = 600

        contract_schema = None
        contract_constraints = None
        if not instruction and experiment_id:
            from pmf_engine.control_plane.registry import EXPERIMENT_REGISTRY
            experiment = EXPERIMENT_REGISTRY.get(experiment_id, {})
            instruction = experiment.get("instruction", "")
            harness = experiment.get("harness", harness)
            model = experiment.get("model", model)
            max_turns = experiment.get("max_turns", max_turns)
            timeout_seconds = experiment.get("timeout_seconds", timeout_seconds)
            contract = experiment.get("contract", {})
            contract_schema = contract.get("schema")
            contract_constraints = contract.get("constraints")

        environment_raw = os.environ.get("ENVIRONMENT", "dev")
        environment_normalized = environment_raw.strip().lower()
        broker_url_raw = os.environ.get("BROKER_URL", "")
        broker_url = broker_url_raw.strip()

        if not broker_url and environment_normalized in _AWS_DEPLOYMENT_ENVS:
            raise BrokerUrlSchemeError(
                f"BROKER_URL must be set in environment={environment_normalized!r}. "
                f"A runner in a deployed environment cannot operate without the broker. "
                f"Set BROKER_URL on the ECS task definition "
                f"(infrastructure/modules/pmf-engine-fargate) to https://broker-{environment_normalized}.ai.goodparty.org."
            )

        scheme = broker_url.split("://", 1)[0].lower() if "://" in broker_url else ""
        if (
            broker_url
            and environment_normalized in _AWS_DEPLOYMENT_ENVS
            and scheme != "https"
        ):
            safe_url = _redact_userinfo(broker_url)
            raise BrokerUrlSchemeError(
                f"BROKER_URL must use https:// in environment={environment_normalized!r}; "
                f"got scheme={scheme!r} url={safe_url!r}. Plaintext http:// is only "
                f"permitted outside {list(_AWS_DEPLOYMENT_ENVS)} (for local in-process broker). "
                f"Set BROKER_URL on the ECS task definition "
                f"(infrastructure/modules/pmf-engine-fargate) to a URL beginning with https://."
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
            contract_constraints=contract_constraints,
            max_turns=max_turns,
            timeout_seconds=int(os.environ.get("TIMEOUT_SECONDS", str(timeout_seconds))),
        )
