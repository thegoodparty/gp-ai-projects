# CLAUDE.md

Guidance for Claude Code and other AI agents working in `gp-ai-projects`. Keep this file short — push detail into `docs/` and per-workspace-member READMEs.

## Project

A **`uv` workspace** holding GoodParty's Python AI / data services. Each workspace member is its own deployable (mostly AWS Lambdas, plus a few CLIs and one FastAPI service). One root `pyproject.toml` + one `uv.lock` resolve all member deps together. Style is enforced by **ruff** (lint + format) and **mypy** (gradual strictness — strict only for `serve.v1_pipeline.*` and `shared.*`); hooks are wired via **pre-commit**.

## Commands (most-used first)

```bash
uv sync                           # install workspace deps (root + all members) into .venv/
make check                        # ruff lint + ruff format + mypy (uv run under the hood)
make lint                         # ruff check .
make format                       # ruff format .
make type-check                   # mypy serve/v1_pipeline/ shared/
make test                         # uv run pytest tests/
uv run pytest <path>              # single-file or single-test run
make install-hooks                # one-time pre-commit setup

# Per-member tests (members ship their own test suites)
cd broker && uv run pytest tests/
cd pmf_engine && uv run pytest tests/
```

CI workflows are mostly `build-*.yml` Docker→ECR builds per workspace member, plus a couple of `deploy-*.yml` flows. There is **no lint/type-check workflow** yet — `.github/workflows/README.md` describes one as if it lives in `.github/workflows-disabled/`, but that directory does not currently exist on disk. Until a real CI lint job lands, **green pre-commit locally is what stands in for lint CI**.

## Pointer table — when in doubt

| Doing                         | Read                                                                                              |
| ----------------------------- | ------------------------------------------------------------------------------------------------- |
| Adding a new workspace member | `docs/architecture.md` § Adding a workspace member                                                |
| First-time setup              | `docs/getting-started.md`                                                                         |
| Working on a specific member  | that member's `README.md` (e.g. `broker/README.md`, `pmf_engine/README.md`)                       |
| Cross-member shared code      | `shared/` (note: this is a workspace member, not just a directory — has its own `pyproject.toml`) |
| AI rule-by-rule code review   | `ai-rules/` (git submodule)                                                                       |
| Why a thing is the way it is  | `docs/adr/` (not yet seeded)                                                                      |

## Code style

- **Python `3.13`** at runtime (`.python-version`). Subprojects declare `requires-python = ">=3.11"`. ruff targets `py311`. mypy `python_version = 3.11`. The 3.13 runtime can run anything ≤3.13; the 3.11 floor is the compatibility line. Don't use 3.12+ syntax features (e.g., PEP 695 type aliases) without checking that ruff and mypy agree.
- **ruff** with `line-length = 120`, `target-version = py311`. Selected rules: `E, W, F, I, B, C4, UP`. Ignored: `E501` (long lines — relying on auto-format), `B008` (function calls in argument defaults — common in FastAPI). Per-file: `__init__.py` ignores `F401` (re-exports).
- **ruff format** is the formatter (no separate black/isort).
- **mypy** is **gradual** — see `mypy.ini`. Whole repo: `check_untyped_defs = True`, `strict_optional = True`, `no_implicit_optional = True`, `warn_unused_ignores`, `warn_redundant_casts`. **Strict** for `serve.v1_pipeline.*` and `shared.*` (`disallow_untyped_defs = True`). Other modules are intentionally loose so we can tighten them gradually.
- `mypy-strict.ini` is a parking lot for tighter rules being rolled out incrementally — don't promote anything to it without coordinating.
- Type hints required on `serve/v1_pipeline/` and `shared/`. Encouraged elsewhere; not yet enforced.

## Workspace shape

```
gp-ai-projects/                  # uv workspace root
├── pyproject.toml               # workspace root (members listed under [tool.uv.workspace])
├── uv.lock                      # the one lockfile
├── .python-version              # 3.13
├── conftest.py                  # autouse fixture to disable Braintrust telemetry in tests
├── mypy.ini / mypy-strict.ini   # gradual mypy; strict-only for serve.v1_pipeline + shared
├── Makefile                     # check / lint / format / type-check / test / hooks
├── .pre-commit-config.yaml      # ruff + ruff-format + mypy (scoped) + hygiene
├── ai-rules/                    # submodule
├── tests/                       # repo-root cross-member tests (currently: ai_generated_campaign_plan)
│
├── shared/                      # WORKSPACE MEMBER — cross-cutting clients & utilities
├── serve/v1_pipeline/           # WORKSPACE MEMBER — pipeline service (FastAPI / SQS-driven)
├── hubspot_ddhq_match/          # WORKSPACE MEMBER — Lambda
├── clickup_bot/                 # WORKSPACE MEMBER — Lambda
├── engineer_agent/              # WORKSPACE MEMBER — Lambda
├── pmf_engine/                  # WORKSPACE MEMBER — Lambda
├── broker/                      # WORKSPACE MEMBER — broker service
│
├── ai_generated_campaign_plan/  # not a member — older module
├── api/                         # not a member — top-level entry points
├── campaign_plan_lambda/        # not a member — Lambda with its own deploy workflow
├── llm_deployment/              # not a member
├── serve/hierarchical_discovery/ # not a member — sibling to serve/v1_pipeline/
├── serve/analyze_texts/         # not a member — sibling to serve/v1_pipeline/
├── serve/classify/              # not a member — sibling to serve/v1_pipeline/
├── infrastructure/              # Terraform (modules + per-env configs) for the lambdas above
├── stitch_golden_data/          # not a member — script-style
├── silver-to-gold-migration/    # not a member — script-style
└── bronze_data/                 # not a member — data drop / config
```

`pmf_engine/` and `engineer_agent/` are good references for adding a new containerized Lambda member: each pairs a `pyproject.toml`, handler, `Dockerfile`, and `tests/` with a corresponding `infrastructure/modules/<name>/` Terraform module.

## Testing

- Framework: **pytest** with `asyncio_mode = auto` (declared in root `pyproject.toml`).
- Top-level `conftest.py` autouse-disables Braintrust telemetry — every test runs with `BRAINTRUST_API_KEY=""` so no test pollutes a live Braintrust project.
- Single test: `uv run pytest <path>::TestClass::test_case -v`.
- Per-member: each workspace member has its own `tests/` and runs them with `cd <member> && uv run pytest tests/`.
- Repo-root suite: `tests/` currently only covers `ai_generated_campaign_plan/`.

## Never

- Never bump a workspace member's deps without running `uv sync` and committing the updated `uv.lock`. The lockfile is the source of truth across all members.
- Never disable the autouse `disable_braintrust` fixture in `conftest.py` — tests would then authenticate against the live Braintrust project. If you need real Braintrust calls in a test, set the env explicitly inside that test only.
- Never edit a file under `ai-rules/` directly — it's a submodule. Changes belong in the upstream `thegoodparty/ai-rules` repo. Bump the pin afterward and stage `ai-rules` in the parent.
- Never copy code from `shared/` into a member by hand. If it's worth using, import it (`shared/` is a workspace member; just declare the dep). Forks rot.
- Never silence mypy with a blanket `# type: ignore` in `serve/v1_pipeline/` or `shared/` — those are strict-mode. Narrow the ignore (`# type: ignore[<error-code>]`) and add a comment explaining why.
- Never remove a workspace member from `[tool.uv.workspace] members` without removing or migrating its code in the same change.

## Environment

- **Python `3.13`** runtime via `uv` (`.python-version`).
- **Package manager: `uv`** (`uv sync` / `uv add` / `uv run`). The `.venv/` lives at the repo root; **don't make per-member venvs**.
- **Required env vars:** see `.env.example`. Real `.env` is gitignored — local-only.
- **AWS / Lambda:** Deployment is via Terraform under `infrastructure/`, plus the per-member Docker→ECR build workflows in `.github/workflows/build-*.yml`. Secrets come from AWS Secrets Manager (e.g., `AI_SECRETS_<ENV>`); never check creds in.
- The `ai-rules/` submodule isn't auto-initialized (no `package.json` postinstall available). After cloning, run `make submodule-init` or `git submodule update --init --recursive`.
