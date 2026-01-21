import asyncio
import os
import sys

from claude_agent_sdk import (
    query,
    ClaudeAgentOptions,
    AssistantMessage,
    UserMessage,
    TextBlock,
    ToolUseBlock,
    ToolResultBlock,
    ResultMessage,
)

from shared.logger import get_logger
from .config import AgentConfig, CAPABILITIES, build_capability_prompt


logger = get_logger(__name__)


def build_system_prompt(instruction: str) -> str:
    capability = build_capability_prompt()
    return capability + "\n" + instruction


def build_task_prompt(config: AgentConfig) -> str:
    return f"""Task ID: {config.task_id}

Complete the task according to your instructions.
"""


async def run_agent(config: AgentConfig) -> dict:
    logger.info(f"Starting agent for task: {config.task_id} (model: {config.model})")

    if not config.instruction:
        logger.error("No INSTRUCTION provided")
        return {
            "status": "error",
            "task_id": config.task_id,
            "error": "No INSTRUCTION provided"
        }

    options = ClaudeAgentOptions(
        system_prompt=build_system_prompt(config.instruction),
        allowed_tools=CAPABILITIES["sdk_tools"],
        permission_mode="bypassPermissions",
        cwd=config.workspace_dir,
        max_turns=200,
        model=config.model,
    )

    prompt = build_task_prompt(config)

    result_text = ""
    total_cost = 0.0
    session_id = None

    message_count = 0
    try:
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                message_count += 1
                for block in message.content:
                    if isinstance(block, TextBlock):
                        logger.info(f"[{message_count}] 💬 {block.text}")
                    elif isinstance(block, ToolUseBlock):
                        logger.info(f"[{message_count}] 🔧 {block.name}: {block.input}")

            elif isinstance(message, UserMessage):
                for block in message.content:
                    if isinstance(block, ToolResultBlock):
                        content = block.content if block.content else "(empty)"
                        status = "❌" if block.is_error else "✅"
                        logger.info(f"[{message_count}] {status} Result: {content}")

            elif isinstance(message, ResultMessage):
                result_text = message.result or ""
                total_cost = message.total_cost_usd or 0.0
                num_turns = message.num_turns
                session_id = message.session_id

                if message.is_error:
                    logger.error(f"Agent ended with error after {num_turns} turns: {result_text}")
                    return {
                        "status": "error",
                        "task_id": config.task_id,
                        "error": result_text,
                        "cost_usd": total_cost,
                        "num_turns": num_turns,
                        "session_id": session_id
                    }

                logger.info(f"Agent completed: {num_turns} turns, {message_count} messages. Cost: ${total_cost:.4f}. Session: {session_id}")
                return {
                    "status": "success",
                    "task_id": config.task_id,
                    "result": result_text,
                    "cost_usd": total_cost,
                    "num_turns": num_turns,
                    "session_id": session_id
                }

    except Exception as e:
        logger.exception(f"Agent failed: {e}")
        return {
            "status": "error",
            "task_id": config.task_id,
            "error": str(e),
            "session_id": session_id
        }


async def main():
    config = AgentConfig.from_env()

    if not config.task_id:
        logger.error("TASK_ID (or CLICKUP_TASK_ID) environment variable required")
        sys.exit(1)

    if not config.instruction:
        logger.error("INSTRUCTION environment variable required")
        sys.exit(1)

    os.makedirs(config.workspace_dir, exist_ok=True)

    result = await run_agent(config)

    logger.info(f"Agent result: {result}")

    if result["status"] == "error":
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
