---
name: python-developer
description: Repo-local guidance for writing Python in gp-ai-projects. Captures the uv-workspace + gradual-mypy reality so agents don't reach for the wrong package manager, run the wrong test command, or break the lockfile. Use whenever a task involves writing or running Python code in this repo.
---

You are writing Python in `gp-ai-projects`. Read this before you `uv add` anything or run any test.

## Ground truth

- This is a **`uv` workspace**, not a collection of independent projects.
- One root `pyproject.toml`, one `uv.lock`, one `.venv/` at the repo root.
- Workspace members are listed under `[tool.uv.workspace] members` in the root `pyproject.toml`. Today: `shared`, `serve/v1_pipeline`, `hubspot_ddhq_match`, `clickup_bot`, `engineer_agent`, `meeting_qa`, `pmf_engine`, `broker`.
- Other directories (e.g. `campaign_plan_lambda/`, `infrastructure/`, `api/`) are part of the repo but **not** workspace members. Their deps come from the root project's `dependencies` (or for `campaign_plan_lambda`, from the `[dependency-groups] campaign-plan-lambda` group).
- Runtime Python is **3.13** (`.python-version`). Code-floor is 3.11 (ruff `target-version`, mypy `python_version`). Don't use 3.12+-only syntax until the targets are bumped.

## Decision tree

- **Adding a new dep used by one member only** → `cd <member> && uv add <package>`. Updates that member's `pyproject.toml` + the root `uv.lock`. Commit both.
- **Adding a new dep used by multiple members** → `cd shared && uv add <package>`, then have other members depend on `shared`.
- **Adding a new top-level dep used outside the workspace members (e.g., for a script in `scripts/` or a non-member dir)** → at the repo root, `uv add <package>` (updates root `pyproject.toml`).
- **Adding a brand-new workspace member** →
  1. Create `<member>/pyproject.toml`.
  2. Append `<member>` to `[tool.uv.workspace] members` in root `pyproject.toml`.
  3. `uv sync` at the root.
  4. Commit `pyproject.toml`, the new `<member>/pyproject.toml`, and the updated `uv.lock` together.
  5. If it's a Lambda, copy the structure from `meeting_qa/` (Dockerfile, `lambda_handler.py`, `tests/`) and add a matching `infrastructure/modules/<name>/`.
- **Running tests** →
  - Repo-root cross-member tests: `uv run pytest tests/` (or `make test`).
  - Per-member: `cd <member> && uv run pytest tests/`.
  - Single test: `uv run pytest <path>::TestClass::test_case -v`.

## Style enforcement

`pyproject.toml` `[tool.ruff]` is the source of truth:

- `line-length = 120`
- `target-version = "py311"`
- Rule selection: `E, W, F, I, B, C4, UP`
- Ignored: `E501` (long lines — relying on auto-format), `B008` (function-call defaults — common in FastAPI)
- Per-file: `__init__.py` ignores `F401` (re-exports)

`ruff format` is the formatter — no separate black / isort. Don't fight it; `make check` is your friend.

`.pre-commit-config.yaml` runs ruff + ruff-format + scoped mypy + hygiene hooks. CI does **not** enforce lint/type-check yet — there is no `lint-and-type-check.yml` workflow on disk (despite what `.github/workflows/README.md` implies). Until that lands, **green pre-commit locally is the bar**.

## Type-checking strategy

`mypy.ini` is **gradual**:

| Path | Strict? |
|------|---------|
| `serve.v1_pipeline.*` | **Yes** (`disallow_untyped_defs`) |
| `shared.*` | **Yes** (`disallow_untyped_defs`) |
| Everything else | **No** (`disallow_untyped_defs = False`) — but `check_untyped_defs = True`, `strict_optional = True`, `no_implicit_optional = True` are on globally |

`mypy-strict.ini` is a parking lot for tighter rules being rolled out incrementally. Don't promote modules into it casually.

When you write new code in a strict module:
- Type **all** function signatures.
- If you must `# type: ignore`, narrow it with the error code: `# type: ignore[union-attr]`. Add a one-line comment for *why*.

When you write new code in a non-strict module:
- Type hints are encouraged, not required. But aim to leave the module in a state where someone could promote it to strict later.

## Common pitfalls

- **`pip install` or `poetry install`.** Wrong tool. This repo is `uv`. Anything you read in old branches that says `pip install -r requirements.txt` is for legacy non-member dirs only (e.g. `apps/genie-slack-bot/` in a *different* repo).
- **Per-member venvs.** There is one `.venv/` at the repo root. Members share it.
- **Forgetting `uv.lock`.** Any change to `pyproject.toml` (root or member) **must** ship with the regenerated `uv.lock`. PR review will catch it; don't.
- **Disabling the autouse Braintrust fixture in `conftest.py`.** Tests would then talk to the live Braintrust project. Don't.
- **Importing `shared` by relative path or copy-paste.** `shared/` is a workspace member; declare it as a dep and `from shared.aws_clients import ...`.
- **3.12+ syntax in code that ruff/mypy target as 3.11.** Both target `py311`. If you want to use newer syntax, bump both targets in lockstep.
- **`# type: ignore` blanket suppression in strict modules.** Narrow it. Comment why.
- **Adding a build workflow to `.github/workflows/` without testing it.** The existing build workflows are per-member. Copy from a similar one (`build-meeting-qa.yml` is recent and clean).
- **Editing `ai-rules/` directly.** Submodule. Changes go upstream.

## What "good" looks like

- You ran `make check` and **read its output** before committing. Note: `make check` runs ruff (lint + format) **enforcing** and mypy in **advisory mode** (`Makefile`'s `type-check` target uses `|| true`, so mypy errors print but don't fail the target). The exit code alone is not a quality signal for types — eyeball the mypy section, or run `uv run mypy serve/v1_pipeline/ shared/` directly.
- If you changed deps, the diff includes both the `pyproject.toml` change and the `uv.lock` update.
- New code in `serve/v1_pipeline/` or `shared/` is fully typed; new code elsewhere is at least linted clean.
- New tests live with the member that owns the code, not at the repo root (unless they're cross-member by design).
- `conftest.py` is unchanged unless you have a specific test-infra reason to change it.
- You didn't reach inside `ai-rules/` or any other submodule.
