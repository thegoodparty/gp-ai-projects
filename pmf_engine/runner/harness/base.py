from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class HarnessResult:
    artifact_bytes: bytes
    content_type: str
    cost_usd: float = 0.0
    num_turns: int = 0
    session_id: str | None = None


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
