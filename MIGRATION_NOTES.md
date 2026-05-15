# Meeting QA — Migration Notes

This branch (`feature/qa-integration`) moves the standalone `meeting_briefings_qa` repo into `gp-ai-projects` as a workspace member named `meeting_qa`, adds an SQS-triggered Lambda handler, a Dockerfile, and the Terraform module + dev environment for it.

## What's in this PR

| Area | Path | Notes |
|------|------|-------|
| QA engine code | `meeting_qa/qa/` | Copied verbatim from `meeting_briefings_qa/qa/` on branch `qa-grounding-improvements`. 31 files, ~2,500 lines. |
| Workspace member | `meeting_qa/pyproject.toml` | Dependencies copied from source repo. Added to root `pyproject.toml` workspace members. |
| Root deps | `pyproject.toml` | Added `anthropic`, `openpyxl`, `pymupdf` so `uv sync` at workspace root has them. |
| CLI | `meeting_qa/scripts/run_qa.py` | Copied from source. **Note: imports `meeting_pipeline.shared.config` — this resolves once the meeting-pipeline branch is merged to develop.** Until then, the CLI on develop will fail to import. The Lambda handler does NOT depend on this module. |
| Lambda handler | `meeting_qa/lambda_handler.py` | New, self-contained. SQS-triggered. Reads briefing/normalized/PDF from S3, runs QA engine, writes outputs to `s3://<bucket>/meeting_pipeline/output/qa/<stem>/`. Loads API keys from Secrets Manager (`AI_SECRETS_<ENV>`) via `_inject_secrets()`. |
| Dockerfile | `meeting_qa/Dockerfile` | Standard Python Lambda image. CMD: `meeting_qa.lambda_handler.handler`. |
| Tests | `meeting_qa/tests/` | All 35 tests pass (`cd meeting_qa && uv run pytest tests/`). |
| Terraform module | `infrastructure/modules/meeting-qa/` | New independent module: SQS queue + DLQ, Lambda function, IAM role, CloudWatch log group, event-source mapping. |
| Terraform dev env | `infrastructure/environments/dev/meeting-qa/` | Independent state at `meeting-qa/dev/terraform.tfstate`. References `shared-infra/dev` for ECR URL. |

## Decisions baked in

| Decision | Choice |
|----------|--------|
| Block behavior | Document failures to S3 (qa_summary.md, review_log.xlsx, trace.json). No delete, no quarantine, no auto-action. Periodic human review. |
| Latency | Async via SQS. Never blocks briefing delivery. |
| Retry | None. Failed QAs sit in S3 for review. |
| Workspace path | `meeting_qa/` at repo root. |
| QA's IAC | Independent module at `infrastructure/modules/meeting-qa/` (not nested inside meeting-pipeline). |
| Lambda handler path | `meeting_qa/lambda_handler.py` → import path `meeting_qa.lambda_handler.handler`. |
| Old repo (`meeting_briefings_qa`) | Untouched. No deprecation work. |

## Required cross-branch changes (must land on `meeting-pipeline` branch before deploy)

The current `infrastructure/modules/meeting-pipeline/main.tf` on the `meeting-pipeline` branch contains QA-specific resources that should now live in this PR's new module. **Before deploying QA, do this on the `meeting-pipeline` branch:**

1. **Remove these resources** from `infrastructure/modules/meeting-pipeline/main.tf`:
   - `aws_cloudwatch_log_group.qa`
   - `aws_sqs_queue.qa_dlq`
   - `aws_sqs_queue.qa`
   - `aws_lambda_function.qa`
   - `aws_lambda_event_source_mapping.qa_sqs`
   - References to `aws_sqs_queue.qa.arn` in the `lambda_permissions` IAM policy (the `sqs:Send/Receive/Delete/GetQueueAttributes` resource list)

2. **Update the process Lambda's `QA_QUEUE_URL`** environment variable to source from the meeting-qa remote state instead of the (now-removed) local resource. Example:
   ```hcl
   # In infrastructure/environments/dev/meeting-pipeline/main.tf:
   data "terraform_remote_state" "meeting_qa" {
     backend = "s3"
     config = {
       bucket = "goodparty-terraform-state-us-west-2"
       key    = "meeting-qa/dev/terraform.tfstate"
       region = "us-west-2"
     }
   }
   # Then pass meeting_qa_queue_url = data.terraform_remote_state.meeting_qa.outputs.queue_url
   # into the meeting-pipeline module, and use that in process Lambda's env block.
   ```

3. **Grant the meeting-pipeline process Lambda's IAM role `sqs:SendMessage`** on the QA queue ARN (from remote state).

4. **Update the meeting-pipeline image command for the QA Lambda was**: the old Terraform had `command = ["qa.lambda_handler.handler"]`. That path no longer exists in the new structure (lambda_handler.py is at `meeting_qa/lambda_handler.py`, not inside `qa/`). The new module here uses `meeting_qa.lambda_handler.handler` correctly.

The wire-up in `meeting_pipeline/lambda_handlers/process.py` (lines 132-137) reads `QA_QUEUE_URL` from env and is already a graceful no-op when empty — no code changes needed there.

## Required CI/CD changes (also on `meeting-pipeline` branch)

The current `.github/workflows/build-meeting-pipeline.yml` builds **one image** with tag `meeting-pipeline-<env>` and updates the `scan` and `process` Lambdas. It does NOT build the QA image.

Two options to add QA image building:

**A. Add a step to build-meeting-pipeline.yml:**
- Add a second `docker/build-push-action` step using `./meeting_qa/Dockerfile` with tag `meeting-qa-<env>`
- Add `meeting-qa-<env>` Lambda update to the loop

**B. Create a separate workflow `.github/workflows/build-meeting-qa.yml`:**
- Same trigger pattern (push to develop/qa/prod)
- Path filter: `meeting_qa/**`, `shared/**`, `pyproject.toml`, `uv.lock`
- Builds with `./meeting_qa/Dockerfile`, tags as `meeting-qa-<env>`, updates only `meeting-qa-<env>` Lambda

I'd lean toward A (single workflow, simpler ops) but B is cleaner for review and lifecycle independence. Either way, this is a small change that should land on the meeting-pipeline branch (where the existing workflow lives) once that branch merges.

## Required AWS Secrets Manager change

The QA Lambda needs `ANTHROPIC_API_KEY` for the Claude triage judge. The existing `AI_SECRETS_DEV` secret currently contains:

- `GEMINI_API_KEY`
- `SERPER_API_KEY`
- `FIRECRAWL_API_KEY`

**Add to that secret:** `ANTHROPIC_API_KEY` (operational task — done via AWS Console or whatever flow you use to manage that secret).

The Lambda's `_inject_secrets()` function loads `AI_SECRETS_<ENV>` and injects both `GEMINI_API_KEY` and `ANTHROPIC_API_KEY` if present.

## Deploy ordering

1. Merge `feature/qa-integration` → `develop` (this PR; ships code + meeting-qa Terraform module + dev env config + Dockerfile)
2. **In a separate session**, on the `meeting-pipeline` branch: do the cross-branch cleanup above (remove QA resources from meeting-pipeline module, add remote-state reference, update CI/CD)
3. Merge `meeting-pipeline` → `develop`
4. Add `ANTHROPIC_API_KEY` to `AI_SECRETS_DEV` secret
5. `terraform apply` for `infrastructure/environments/dev/meeting-qa/` (creates QA queue + Lambda — Lambda will fail until step 6)
6. CI builds the QA image; Lambda starts working
7. `terraform apply` for `infrastructure/environments/dev/meeting-pipeline/` (process Lambda env now reads QA queue URL from remote state)

After this, briefing generation in dev will dispatch to QA automatically.

## What was NOT changed

- `meeting_briefings_qa/` (the source repo) — untouched. No deprecation note added.
- `meeting_pipeline/lambda_handlers/process.py` — wire-up already exists on the meeting-pipeline branch. Not touched here.
- `meeting_pipeline/lambda_handlers/_secrets.py` — doesn't need to know about ANTHROPIC_API_KEY because the QA Lambda has its own `_inject_secrets()`. The pipeline-side secrets file can stay as-is.

## Validation done

- `uv sync` at workspace root: succeeds
- `uv run python -c "from meeting_qa.lambda_handler import handler"`: succeeds
- `uv run pytest meeting_qa/tests/`: **35 passed**
- Terraform: not validated locally (no terraform binary in this environment). Module syntax follows the meeting-pipeline pattern; `terraform init && terraform validate` should be run before first apply.

## Commits in this branch

1. Add meeting_qa workspace member with QA engine code (copied from `meeting_briefings_qa@qa-grounding-improvements`)
2. Add Lambda handler and Dockerfile for the QA Lambda
3. Add meeting-qa Terraform module + dev environment
4. Add MIGRATION_NOTES.md (this file)
