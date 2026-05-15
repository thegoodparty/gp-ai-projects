# Getting Started

First-time setup for `gp-ai-projects` on macOS / Linux.

## Prerequisites

- **`uv`** — install via `brew install uv` (macOS) or `curl -LsSf https://astral.sh/uv/install.sh | sh`. `uv` will manage Python and the workspace venv for you.
- **Python 3.13** — `uv` will fetch it the first time you run `uv sync` if it's not on your machine.
- **Docker** — only if you'll run a Lambda image locally or rebuild ECR artifacts.
- **AWS access** — only if you'll touch `infrastructure/` (Terraform) or run anything that hits S3 / SQS / Secrets Manager.

You don't need pip / poetry / pyenv for this repo. `uv` covers all of it.

## Clone

This repo uses `ai-rules` as a git submodule. Clone with `--recursive`:

```bash
git clone --recursive git@github.com:thegoodparty/gp-ai-projects.git
cd gp-ai-projects
```

If you already cloned without `--recursive`:

```bash
make submodule-init    # or: git submodule update --init --recursive
```

## Configure environment

Real env values are gitignored. Copy `.env.example` to `.env` and fill in the relevant keys:

```bash
cp .env.example .env
```

`.env.example` ships the shared baseline: `TAVILY_API_KEY`, `GEMINI_API_KEY`, `DATABRICKS_API_KEY`, `DATABRICKS_SERVER_HOSTNAME`, `DATABRICKS_HTTP_PATH`, `GOODPARTY_API_TOKEN`, `BRAINTRUST_API_KEY`, `ENVIRONMENT`. Most workspace members need only a subset of these — check the relevant member's `README.md` for what *that* member actually reads.

Other keys may be required by specific members (e.g., AWS profile / creds for boto3 if you're touching infra, or vendor keys like `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, ClickUp tokens for the bot). Add them to your local `.env` as needed; if the key becomes broadly relevant, also add a placeholder to `.env.example` so the next person knows it exists.

Local tests do **not** need `BRAINTRUST_API_KEY` — `conftest.py` clears it via an autouse fixture so tests never touch a live Braintrust project.

## Install

```bash
uv sync
```

This:
1. Resolves the root `pyproject.toml` plus every workspace member.
2. Materializes `.venv/` at the repo root.
3. Uses the Python version pinned in `.python-version` (3.13).

You don't need to "activate" the venv for `uv run …` commands — `uv` injects the right Python automatically. If you prefer an activated shell:

```bash
source .venv/bin/activate
```

## One-time hooks

```bash
make install-hooks
```

This runs `uv add --dev pre-commit && uv run pre-commit install`. From here on, every `git commit` runs ruff + ruff-format + mypy (scoped to `serve/v1_pipeline/` and `shared/`) + standard hygiene hooks.

To run them manually:

```bash
uv run pre-commit run --all-files
```

## Verify (the same things `make check` runs)

```bash
make lint           # uv run ruff check .
make format         # uv run ruff format .
make type-check     # uv run mypy serve/v1_pipeline/ shared/
make check          # all three of the above
make test           # uv run pytest tests/
```

Per-member tests:

```bash
cd meeting_qa && uv run pytest tests/
cd pmf_engine && uv run pytest tests/
# ... etc
```

## Working on a specific workspace member

```bash
cd <member>
uv run pytest tests/        # run that member's tests
uv run python -m <module>   # invoke its entry point if it has one
```

Adding a dep that **only** that member needs:
```bash
cd <member>
uv add <package>            # updates <member>/pyproject.toml + root uv.lock
```

Adding a dep that **multiple** members will use:
```bash
cd shared
uv add <package>            # add to shared, then have other members depend on shared
```

Always commit the updated `uv.lock` along with the dep change.

## Running a Lambda locally

Each Lambda member has a `Dockerfile`. To run it locally:

```bash
cd <member>
docker build -t <member>:local .
docker run --rm --env-file ../.env <member>:local <event-json>
```

For SQS-driven members, you'll also want to look at the corresponding `infrastructure/modules/<member>/main.tf` to see what env the Lambda actually receives in deployed environments.

## Talking to Braintrust intentionally (non-test)

The autouse `disable_braintrust` fixture only fires under `pytest`. Outside tests, set `BRAINTRUST_API_KEY` in your env and the `BraintrustClient` will activate normally.

If you need real Braintrust calls **inside** a test (rare), set the key in that one test's body via `monkeypatch.setenv(...)` after the fixture has run, and reset the singleton at the end. **Don't** disable the autouse fixture globally.

## Common gotchas

- **`ai-rules/` is empty after clone** → run `make submodule-init` or `git submodule update --init --recursive`. There's no postinstall hook.
- **`uv sync` fails with a Python version mismatch** → confirm `.python-version` is `3.13` and that `uv python install 3.13` has run (or just rerun `uv sync`; `uv` will fetch it).
- **mypy passes locally but PR review flags type errors** → mypy is scoped to `serve/v1_pipeline/` and `shared/` in `.pre-commit-config.yaml`. If you've added strict-mypy modules, update the `files:` regex in the config.
- **Tests want to talk to Braintrust** → check `conftest.py`. The autouse fixture should keep it disabled. If a test sets `BRAINTRUST_API_KEY` itself, it must reset the singleton.
- **`uv add` doesn't update the lockfile** → make sure you're inside a workspace dir (root or any member). `uv add` outside the workspace falls back to a different mode.
- **CI build for a member fails on a transitive dep** → the CI image is built per-member; if your member depends on a new lib, ensure it's in *that member's* `pyproject.toml`, not just the root project's `dependencies`.
- **Secrets ended up in `.env`** → `.env` is gitignored; do not commit. Use `.env.example` for shape only.

## Where to go next

- `README.md` — repo overview.
- `CLAUDE.md` — agent + style guide.
- `docs/architecture.md` — workspace shape, member catalog, data flow, CI map.
- `MIGRATION_NOTES.md` — most recent example of migrating a standalone module into the workspace (`meeting_qa/`).
- `ai-rules/` — org-wide review rules and skills (submodule).
