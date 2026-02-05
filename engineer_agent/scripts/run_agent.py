import argparse
import asyncio
import os
import sys

from shared.logger import get_logger
from engineer_agent.agent import run_agent
from engineer_agent.agent.config import AgentConfig


logger = get_logger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Run the engineer agent locally")
    parser.add_argument(
        "--task-id",
        type=str,
        required=True,
        help="ClickUp task ID to analyze"
    )
    parser.add_argument(
        "--instruction",
        type=str,
        default=None,
        help="Instruction text (or use --instruction-file)"
    )
    parser.add_argument(
        "--instruction-file",
        type=str,
        default=None,
        help="Path to file containing instruction"
    )
    parser.add_argument(
        "--workspace",
        type=str,
        default="/tmp/engineer-workspace",
        help="Workspace directory for cloning repos"
    )

    args = parser.parse_args()

    instruction = args.instruction
    if args.instruction_file:
        with open(args.instruction_file) as f:
            instruction = f.read()
    if not instruction:
        logger.error("No instruction provided. Use --instruction or --instruction-file")
        sys.exit(1)

    os.makedirs(args.workspace, exist_ok=True)

    config = AgentConfig(
        task_id=args.task_id,
        instruction=instruction,
        environment="development",
        workspace_dir=args.workspace,
    )

    logger.info(f"Running agent for task: {config.task_id}")
    logger.info(f"Workspace: {config.workspace_dir}")

    result = asyncio.run(run_agent(config))

    print("\n" + "=" * 50)
    print("Agent Result:")
    print("=" * 50)
    print(f"Status: {result['status']}")
    print(f"Task ID: {result['task_id']}")
    if "cost_usd" in result:
        print(f"Cost: ${result['cost_usd']:.4f}")
    if "error" in result:
        print(f"Error: {result['error']}")
    print("=" * 50)


if __name__ == "__main__":
    main()
