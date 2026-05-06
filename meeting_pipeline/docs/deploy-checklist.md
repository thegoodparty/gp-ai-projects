# Deploy checklist — first end-to-end deploy

Recipe for the first time the three branches `meeting-pipeline`, `meeting-pipeline-infra`, and `feature/qa-integration` are merged to `develop` and the stack is brought up in the **dev** environment. Prod follows the same recipe but only after dev is healthy (see §8).

## 0. One-time operational prep

Do anytime before §3.

- [ ] **Add `ANTHROPIC_API_KEY` to `AI_SECRETS_DEV`** in AWS Secrets Manager.
  AWS Console → Secrets Manager → `AI_SECRETS_DEV` → Retrieve → Edit → add JSON key `ANTHROPIC_API_KEY` with the real Claude API key.
  The QA Lambda's IAM role already has `secretsmanager:GetSecretValue` granted; no IAM changes needed.
- [ ] **Confirm `AI_SECRETS_DEV` already has** `GEMINI_API_KEY`, `SERPER_API_KEY`, `FIRECRAWL_API_KEY`. (Required for scan / process / discover.)

## 1. Merge order

The infra branch is stacked on the app branch, so meeting-pipeline must merge first.

- [ ] **Merge `meeting-pipeline` → `develop`** first. *No CI fires* — the `build-meeting-pipeline.yml` workflow file lives on the infra branch, not this one.
- [ ] **Merge `meeting-pipeline-infra` → `develop`** second. *CI fires:*
  - Image build + ECR push → succeeds.
  - `aws lambda update-function-code` → fails (Lambdas don't exist yet — terraform creates them in §2).
  - `aws ecs update-service` → fails (service doesn't exist yet).
  - Workflow run goes red. **Expected on first deploy.** Re-run after §2 and it passes.
- [ ] **Merge `feature/qa-integration` → `develop`** third. *CI fires:*
  - Image build + ECR push → succeeds.
  - `aws lambda update-function-code` for `meeting-qa-dev` → fails (same reason).
  - Red. Same fix-up after §3.

## 2. Apply pipeline terraform (your laptop)

Prerequisites:
- `AWS_PROFILE=goodparty` exported, SSO active (`aws sso login --profile goodparty`).
- Terraform CLI ≥ 1.10 (`terraform version`).

Steps:

- [ ] `cd infrastructure/environments/dev/meeting-pipeline`
- [ ] If `terraform.tfvars` doesn't exist locally, `cp terraform.tfvars.example terraform.tfvars` and fill in:
  ```
  vpc_id            = "vpc-0763fa52c32ebcf6a"
  public_subnet_ids = ["subnet-07984b965dabfdedc", "subnet-01c540e6428cdd8db"]
  ```
- [ ] `terraform init` — downloads providers, sets up the S3 backend lock.
- [ ] `terraform plan`. Expected: ~35 adds (Lambdas, SQS queues, Step Function, ECS cluster + task + service, alarms, SNS topic, S3 bucket access-block + SSE config), 1 in-place tag-only update on the imported bucket. **No destroys.**
- [ ] `terraform apply`. The discover ECS service starts pulling its image as soon as the task definition is registered; if CI hasn't pushed the image yet, the service crash-loops until it does.

## 3. Apply QA terraform (your laptop)

- [ ] `cd ../meeting-qa` (still in `infrastructure/environments/dev/`)
- [ ] No tfvars file needed — only required vars are `environment` (hardcoded) and `ecr_repository_url` (from remote state).
- [ ] `terraform init && terraform plan && terraform apply`. Creates SQS queue + DLQ, Lambda, IAM role, log group, SNS topic + Slack subscription, DLQ alarm, event-source mapping.

## 4. Re-run the failed CI workflows

Both workflows failed on first merge because Lambdas/ECS didn't exist yet. Now they do.

- [ ] GitHub → Actions → **Build and Deploy Meeting Pipeline** → most recent run on `develop` → "Re-run failed jobs" (or "Run workflow" → branch=`develop` → manual dispatch).
- [ ] Same for **Build and Deploy Meeting QA**.
- [ ] Verify both go green.

## 5. Wire pipeline → QA queue (one final terraform apply)

The pipeline doesn't yet know where to send briefings for QA. `infrastructure/environments/dev/meeting-pipeline/main.tf` currently has a comment that says *"qa_queue_url / qa_queue_arn intentionally omitted — see note above."* Replace it with a remote-state reference + module input.

- [ ] On `develop` (locally), edit `infrastructure/environments/dev/meeting-pipeline/main.tf`:
  ```hcl
  # Add near the other remote-state blocks:
  data "terraform_remote_state" "meeting_qa" {
    backend = "s3"
    config = {
      bucket = "goodparty-terraform-state-us-west-2"
      key    = "meeting-qa/dev/terraform.tfstate"
      region = "us-west-2"
    }
  }
  ```
  Inside the `module "meeting_pipeline"` block, add:
  ```hcl
  qa_queue_url = data.terraform_remote_state.meeting_qa.outputs.queue_url
  qa_queue_arn = data.terraform_remote_state.meeting_qa.outputs.queue_arn
  ```
  Delete the `# qa_queue_url / qa_queue_arn intentionally omitted` comment.
- [ ] `terraform plan` from that env. Expected: process Lambda env var `QA_QUEUE_URL` populated, lambda IAM policy gains `sqs:SendMessage` on the QA queue ARN. ~2 in-place updates, 0 destroys.
- [ ] `terraform apply`.
- [ ] Commit + push to `develop`:
  ```bash
  git add infrastructure/environments/dev/meeting-pipeline/main.tf
  git commit -m "Wire process Lambda to meeting-qa SQS queue"
  git push origin develop
  ```

## 6. End-to-end verification

- [ ] CloudWatch Logs → `/aws/lambda/meeting-pipeline-process-dev` — no errors.
- [ ] CloudWatch Logs → `/aws/lambda/meeting-pipeline-scan-dev` — no errors.
- [ ] CloudWatch Logs → `/ecs/meeting-pipeline-discover-dev` — task running, polling SQS.
- [ ] ECS Console → `meeting-pipeline-discover-dev` cluster → service → 1 running task, deployment status `stable`.
- [ ] **Trigger a real scan.** AWS Console → Step Functions → `meeting-pipeline-scan-fanout-dev` → Start execution with input `{}`. The Map state fans out across cities; each new posted meeting fires the process Lambda; process Lambda writes a briefing and dispatches to QA.
- [ ] **Check QA outputs.** S3 → `s3://meeting-pipeline-dev/meeting_pipeline/output/qa/{slug}_{date}/` — should contain `qa_summary.md`, `review_log.xlsx`, `trace.json` after a few minutes per briefing.
- [ ] **Smoke-check Slack.** Send a malformed message to one of the queues, let it fail 3× into the DLQ, wait ~5 min for the CloudWatch alarm to fire, watch the shared Slack notifier post.

## 7. Things that should *not* trip you up but might

- **EventBridge cron.** Daily at 06:00 UTC the Step Function fires automatically. Runs you didn't trigger are expected.
- **First ECS image pull is slow.** Cold pull from ECR takes 1–2 minutes. `RunningTaskCount=0` during that window is normal.
- **`BucketKeyEnabled` drift after §5.** The imported S3 bucket has `BucketKeyEnabled=true`; the module's SSE config doesn't set it explicitly. Either add `bucket_key_enabled = true` to the module or accept the toggle. With AES256 there's no functional or cost difference.
- **ANTHROPIC_API_KEY missing in dev.** QA Lambda crashes on first invocation with a clear error in CloudWatch. If §0 was skipped, do it and the next run works.

## 8. Prod (later)

When dev is healthy and you want prod:

- [ ] Create `AI_SECRETS_PROD` with all four keys (Gemini, Serper, Firecrawl, Anthropic).
- [ ] Create `infrastructure/environments/prod/meeting-pipeline/main.tf` and `.../prod/meeting-qa/main.tf` mirroring the dev versions. Different state keys (`meeting-pipeline/prod/terraform.tfstate`, `meeting-qa/prod/terraform.tfstate`), `environment = "prod"`, prod VPC + public subnet IDs in `terraform.tfvars`.
- [ ] Create the `meeting-pipeline-prod` S3 bucket beforehand or let terraform create it on first apply (no import needed if it doesn't exist yet).
- [ ] Same merge → terraform apply → CI re-run → wire-up → verify cycle.
