from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from shared.logger import get_logger

logger = get_logger(__name__)


@dataclass
class RunnerConfig:
    experiment_id: str
    run_id: str
    candidate_id: str
    instruction: str
    params: dict = field(default_factory=dict)
    harness: str = "claude_sdk"
    model: str = "sonnet"
    environment: str = "dev"
    artifact_bucket: str = ""
    artifact_key_template: str = ""
    callback_queue_url: str = ""
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

        return cls(
            experiment_id=experiment_id,
            run_id=os.environ.get("RUN_ID", ""),
            candidate_id=os.environ.get("CANDIDATE_ID", ""),
            instruction=instruction,
            params=params,
            harness=harness,
            model=model,
            environment=os.environ.get("ENVIRONMENT", "dev"),
            artifact_bucket=os.environ.get("ARTIFACT_BUCKET", ""),
            artifact_key_template=os.environ.get("ARTIFACT_KEY_TEMPLATE", ""),
            callback_queue_url=os.environ.get("CALLBACK_QUEUE_URL", ""),
            contract_schema=contract_schema,
            contract_constraints=contract_constraints,
            max_turns=max_turns,
            timeout_seconds=int(os.environ.get("TIMEOUT_SECONDS", str(timeout_seconds))),
        )

    def resolve_artifact_key(self) -> str:
        if not self.artifact_key_template:
            return ""
        return (
            self.artifact_key_template
            .replace("{experiment_id}", self.experiment_id)
            .replace("{run_id}", self.run_id)
        )
