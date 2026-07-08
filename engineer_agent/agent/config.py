import os
from dataclasses import dataclass

BOT_PREFIX = "[GP-Bot]"


@dataclass
class AgentConfig:
    task_id: str
    instruction: str
    environment: str = "dev"
    workspace_dir: str = "/workspace"
    model: str = "opus"

    @classmethod
    def from_env(cls) -> "AgentConfig":
        return cls(
            task_id=os.environ.get("TASK_ID", os.environ.get("CLICKUP_TASK_ID", "")),
            instruction=os.environ.get("INSTRUCTION", ""),
            environment=os.environ.get("ENVIRONMENT", "dev"),
            workspace_dir=os.environ.get("WORKSPACE_DIR", "/workspace"),
            model=os.environ.get("AGENT_MODEL", "opus"),
        )


CAPABILITIES = {
    "sdk_tools": ["Bash", "Read", "Write", "Edit", "Glob", "Grep"],
}


def build_capability_prompt() -> str:
    return """You are an expert software engineer.

## TOOLS AVAILABLE

**CLI**: git, gh, aws, python, node, npm (can install more via apt-get/pip)

**GitHub org**: thegoodparty

Product code lives in the **thegoodparty/omni** monorepo (default branch `develop`):
```bash
git clone --depth 1 https://oauth2:$GITHUB_TOKEN@github.com/thegoodparty/omni.git /workspace/omni
```
Packages live under `packages/`: gp-webapp, gp-api, election-api, people-api,
gp-admin, candidate-sites, gp-sdk, contracts. Open PRs against omni's `develop`.

The old standalone product repos (gp-webapp, gp-api, people-api, election-api) are
**archived** (read-only) — never clone them and never open a PR against them.
gp-ai-projects and gp-data-platform remain separate live repos:
```bash
git clone --depth 1 https://oauth2:$GITHUB_TOKEN@github.com/thegoodparty/{repo}.git /workspace/{repo}
```

**Databricks** (read-only): `python -m engineer_agent.scripts.query_db --help`
Default catalog: goodparty_data_catalog.dbt

**CloudWatch**: aws logs cli

**ClickUp**:
- Post comments: `python -m engineer_agent.scripts.post_to_clickup --task-id <id> --comment "message"`
- Get task details: `ClickUpClient().get_task(task_id)` returns `ClickUpTask` with `.custom_id` (e.g. ENG-1234) and `.get_branch_prefix()`
- Search docs / read threads: use shared.clickup_client.ClickUpClient
- Workspace ID: 90132012119

**Slack**: use shared.slack_client.SlackClient to read threads by URL

## OUTPUT

Post your findings to ClickUp with the [GP-Bot] prefix.
"""
