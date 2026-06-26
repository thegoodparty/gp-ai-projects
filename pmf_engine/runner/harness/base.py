from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable


@dataclass
class HarnessResult:
    artifact_bytes: bytes
    content_type: str
    cost_usd: float = 0.0
    num_turns: int = 0
    session_id: str | None = None


@dataclass(frozen=True)
class EvaluatorHarnessParams:
    """Inputs the QA-gate engine hands to the evaluator runner.

    Shared by LANE A (the gate engine, which constructs these and injects a
    fake runner in tests) and the wiring lane (which adapts the real
    `harness.run_evaluator` into the `evaluator_runner` callable). Lives here
    so both lanes import the one shape and compose without drift.

    `system_prompt` REPLACES the capability prompt entirely (the evaluator gets
    no capability section, no manifest preamble, no instruction concatenation).
    `gate_cwd` is the gate's private dir, used as the agent cwd so `/workspace`
    is read-only evidence. `result_file_path` is where the evaluator writes its
    JSON fragment array; the engine reads the fragments back from that path.
    """

    model: str
    max_turns: int
    timeout_seconds: int
    instruction: str
    system_prompt: str
    result_file_path: str
    gate_cwd: str
    workspace_dir: str


@dataclass
class EvaluatorResult:
    """Output of the evaluator runner, consumed by the QA-gate engine.

    `status` is 'ok' when the evaluator ran to completion (even if its
    fragments contain failing checks) and 'error' when the runner itself
    failed. `fragments` is the raw fragment array the evaluator produced (the
    engine still reads the canonical copy from `result_file_path`); the cost /
    duration / turn / session metrics are gate-own accounting (decision 12).
    """

    fragments: list[dict] = field(default_factory=list)
    cost_usd: float = 0.0
    duration_ms: int = 0
    num_turns: int = 0
    session_id: str | None = None
    status: Literal["ok", "error"] = "ok"
    eval_transcript: str = ""


@runtime_checkable
class AgentHarness(Protocol):
    async def run(
        self,
        instruction: str,
        model: str,
        max_turns: int,
        workspace_dir: str,
        params: dict,
        contract_schema: dict | None = None,
        parent_span=None,
        experiment_id: str | None = None,
        system_prompt: str | None = None,
        permission_mode: str | None = None,
        allowed_external_tools: list[str] | None = None,
        max_parallel_subagents: int = 0,
        max_thinking_tokens: int | None = None,
    ) -> HarnessResult: ...
