# Repo Context and Non-Negotiables

This project:

- Uses `uv` for environment and execution (do NOT replace with pip/poetry).
- Has `pyproject.toml` and `.python-version` defining Python version and tooling. Infer versions from those files before proposing changes.
- Uses `.env` for secrets and API keys (`.env.example` shows expected vars).
- Uses `ENVIRONMENT` to control logging mode (e.g., "development" vs "production").
- Has runnable scripts under `ai_generated_campaign_plan/sections/` and shared helpers like `api_wrapper.py`.

Non-negotiable constraints:

- Preserve `uv` commands in docs, scripts, and CI (`uv run`, `uv sync`).
- Never log or print secrets or access tokens. Redact if necessary.
- Do not break existing CLI flows (e.g., `uv run ai_generated_campaign_plan/sections/one_overview.py`).
- Keep file paths portable (use `pathlib`, no hard-coded CWD assumptions).
- Prefer incremental, well-scoped PRs with tests.

How to work:

1. Before edits, read `pyproject.toml`, `.python-version`, and touched modules to align with current versions and dependencies.
2. Propose minimal-dependency solutions first. If adding a dependency is justified, update `pyproject.toml` and explain why.
3. When refactoring, keep behavior identical unless explicitly asked to change it. Add tests to lock in behavior.
4. If ambiguity exists, ask clarifying questions in the PR description or comments.
