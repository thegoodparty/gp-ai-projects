# Architecture

A pointer-heavy doc. Detailed conventions live in `CLAUDE.md` and `ai-rules/`.

## What this repo is

A **`uv` workspace** for GoodParty's Python AI / data services. Members are mostly AWS Lambdas (SQS- or HTTP-triggered) plus one FastAPI-based pipeline service and a few CLIs. Everything resolves into a single root `uv.lock`, runs on Python 3.13 locally, and ships as Docker images to ECR via per-member GitHub Actions workflows.

## Stack

- **Python 3.13** (`.python-version`) at runtime; `requires-python = ">=3.11"` floor; ruff and mypy target `py311`.
- **`uv`** for dependency management, lockfile, and venv. `.venv/` at the repo root.
- **`ruff`** for lint + format. `line-length = 120`. Rule selection in `[tool.ruff.lint]`: `E, W, F, I, B, C4, UP`.
- **`mypy`** with **gradual strictness**:
  - Whole-repo: `strict_optional`, `no_implicit_optional`, `check_untyped_defs`, `warn_unused_ignores`, `warn_redundant_casts`.
  - **Strict** for `serve.v1_pipeline.*` and `shared.*` (`disallow_untyped_defs`).
  - `mypy-strict.ini` is a parking lot for tighter rules; promote modules into it incrementally.
- **`pytest`** with `asyncio_mode = auto`. Repo-root `conftest.py` auto-disables Braintrust telemetry.
- **`pre-commit`** hooks: ruff + ruff-format + mypy (scoped to `serve/v1_pipeline/` and `shared/`) + standard hygiene hooks.
- **AI/LLM clients:** anthropic, openai, google-genai, claude-agent-sdk; HTTP via httpx / aiohttp. Telemetry via Braintrust (disabled in tests).
- **Cloud:** AWS — SQS, Lambda, S3, Secrets Manager. Deployment is Terraform under `infrastructure/`. CI builds Docker images to ECR.
- **Other vendors used by members:** Databricks (`databricks-sql-connector`), Slack (`slack-sdk`), HubSpot, ClickUp, Tavily, Google APIs.

## Workspace members

| Member                | Type                  | What it does                                                                                                                                                     |
| --------------------- | --------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `shared/`             | Library               | Cross-cutting clients: `aws_clients`, `braintrust`, `clickup_client`, `databricks_client`, `google_sheets_client`, `llm_gemini_3` / `llm_gemini`, plus utilities |
| `serve/v1_pipeline/`  | FastAPI / SQS service | Main pipeline service — strict mypy                                                                                                                              |
| `hubspot_ddhq_match/` | Lambda                | Match HubSpot contacts to DDHQ records                                                                                                                           |
| `clickup_bot/`        | Lambda                | ClickUp automation                                                                                                                                               |
| `engineer_agent/`     | Lambda                | Engineer-style coding agent                                                                                                                                      |
| `pmf_engine/`         | Lambda                | Product/market-fit engine                                                                                                                                        |
| `broker/`             | Service               | Message broker service                                                                                                                                           |

Members are listed under `[tool.uv.workspace] members` in the root `pyproject.toml`.

## Non-member directories

These are part of the repo but **not** workspace members. Their deps come from the root project's `dependencies` (or their own `requirements.txt` for the legacy ones):

- `ai_generated_campaign_plan/` — older module, has tests at `tests/ai_generated_campaign_plan/`.
- `api/` — top-level entry points.
- `campaign_plan_lambda/` — separate Lambda with its own deploy workflow (`.github/workflows/deploy-campaign-plan-lambda.yml`); has its own dependency group at `[dependency-groups] campaign-plan-lambda`.
- `llm_deployment/` — LLM hosting / deployment helpers.
- `serve/hierarchical_discovery/`, `serve/analyze_texts/`, `serve/classify/` — siblings to `serve/v1_pipeline/` but **not** workspace members. They live under `serve/` for historical/topical reasons; only `serve/v1_pipeline/` is in `[tool.uv.workspace] members`.
- `infrastructure/` — Terraform (modules + per-environment configs) for the AWS resources behind the lambdas.
- `stitch_golden_data/`, `silver-to-gold-migration/`, `bronze_data/` — script-style data movers / configs.

When in doubt about whether a directory is a workspace member, check `pyproject.toml` `[tool.uv.workspace] members`.

## Module shape (per workspace member)

```
<member>/
├── pyproject.toml              # uv workspace member; depends on shared/ and external libs
├── lambda_handler.py           # for SQS/HTTP-triggered Lambdas — entry point
├── Dockerfile                  # for members deployed as ECR images
├── <member>/                   # source package (or no inner subdir for small members)
├── scripts/                    # CLI / one-off helpers
├── tests/                      # pytest suite
└── README.md                   # member-specific runbook
```

`pmf_engine/` and `engineer_agent/` are good examples: each pairs the member code (`pyproject.toml`, handler, `Dockerfile`, `tests/`) with a matching `infrastructure/modules/<name>/` Terraform module.

## Adding a workspace member

1. Pick a name and create the directory `<member>/` at the repo root.
2. Create `<member>/pyproject.toml` with the member's deps. Keep `requires-python` consistent with the root (`>=3.11`).
3. Add the member's path to `[tool.uv.workspace] members` in the root `pyproject.toml`.
4. Run `uv sync` at the repo root and commit the updated `uv.lock`.
5. Move any deps the member needs but doesn't yet have to its `pyproject.toml`. If multiple members will use a dep, add it to **`shared/pyproject.toml`** instead and have other members depend on `shared`.
6. Add the member's tests under `<member>/tests/` (not the root `tests/`).
7. If the member is a deployable Lambda, add its `Dockerfile` and a corresponding `.github/workflows/build-<member>.yml` — copy from an existing build workflow.
8. If it needs AWS infra, add an `infrastructure/modules/<name>/` Terraform module + `infrastructure/environments/dev/<name>/` env.

## Cross-member sharing: `shared/`

`shared/` is a workspace member, **not** just a directory. Other members depend on `shared` as a normal dep. **Don't** copy code from `shared/` into a member — import it. The autouse Braintrust-disabling fixture in `conftest.py` imports `BraintrustClient` from `shared.braintrust`, so adding tests in any member benefits from the same telemetry isolation.

## Data flow (high level)

```
Trigger (SQS / S3 event / HTTP / cron)
   │
   ▼
Lambda handler (e.g. pmf_engine/control_plane/dispatch_handler.py)
   │
   ├─ inject_secrets() ← AWS Secrets Manager (AI_SECRETS_<ENV>)
   │
   ├─ shared.* (clients to AWS, Databricks, Braintrust, LLM providers)
   │
   ├─ member-specific logic (engineer_agent/, broker/, pmf_engine/, ...)
   │
   ├─ LLM calls (anthropic / openai / google-genai)
   │
   └─ side effects:
       ├── S3 outputs
       ├── SQS hand-off to next stage
       └── Braintrust telemetry (disabled in tests)
```

Failures generally **do not** retry automatically — outputs (success or failure trace) land in S3 and a human reviews periodically.

## Cross-service edges

| Direction | Service                            | Protocol                   | Notes                                                    |
| --------- | ---------------------------------- | -------------------------- | -------------------------------------------------------- |
| outbound  | AWS S3 / SQS / Secrets Manager     | `boto3`                    | Per-member IAM via Terraform                             |
| outbound  | Anthropic / OpenAI / Google GenAI  | HTTP                       | LLM calls; token use traced via Braintrust (in non-test) |
| outbound  | Databricks SQL warehouse           | `databricks-sql-connector` | Mostly read-side                                         |
| outbound  | HubSpot, ClickUp, Slack, Tavily    | HTTP                       | Member-specific clients in `shared/` or member dirs      |
| inbound   | SQS / HTTP API Gateway / S3 events | AWS Lambda triggers        | Per-member; defined in Terraform                         |

There is no inbound HTTP API exposed to GoodParty's web frontends from this repo (those go to `gp-api`). The lambdas are internal back-end workers.

## CI

`.github/workflows/`:

- `build-broker.yml`, `build-ddhq-matcher.yml`, `build-engineer-agent.yml`, `build-pmf-engine.yml`, `build-serve-analyze.yml` — per-member Docker→ECR builds. Triggered on changes to that member's path (or shared dependencies).
- `deploy-campaign-plan-lambda.yml` — separate deploy flow for `campaign_plan_lambda/`.
- `deploy-clickup-bot.yml` — separate deploy flow for `clickup_bot/`.

There is **no lint/type-check workflow** in CI yet. `.github/workflows/README.md` references one as if it lived in `.github/workflows-disabled/`, but that directory does not currently exist on disk. Until a real CI lint job is added, **green pre-commit locally is the only enforcement of style + types in this repo's CI**.

## Skills

- `.claude/agents/code-critic.md` — strict reviewer that applies `ai-rules/` + `CLAUDE.md` + the actual ruff/mypy config.
- `.claude/skills/python-developer.md` — captures the workspace + uv reality so agents don't reach for the wrong package manager or run the wrong test command.

## ADRs

`docs/adr/` is not yet seeded. Add one when a non-obvious decision lands — likely candidates: why uv workspace instead of separate repos, why gradual mypy strictness instead of all-or-nothing, why Braintrust as the single observability layer, why per-member ECR images instead of a shared Lambda layer. Use `ai-rules/adr-template.md`.
