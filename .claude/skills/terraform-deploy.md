---
name: terraform-deploy
description: Use when deploying a gp-ai-projects code or infra change to an environment (dev/qa/prod) via Terraform — especially control-plane Lambda changes (dispatch_handler.py, scheduler_handler.py, task_reaper.py) that a branch merge does NOT auto-deploy. Covers the build_lambda_package + AWS-credential bridge + init/plan/apply procedure and what deploys automatically vs. manually.
---

You are deploying a change in `gp-ai-projects`. Read this before assuming a merge deployed your code — for some components it did, for others it did not.

## What deploys automatically vs. needs Terraform

A push/merge to a deployment branch (`develop`→dev, `qa`→qa, `prod`→prod) triggers `build-*.yml` workflows that build+push images. That is the WHOLE deploy for some components and only HALF for others:

| Component | How its code goes live | Action on a code change |
| --- | --- | --- |
| **Broker** (`broker/**`) | `build-broker.yml` builds the image and runs `aws ecs update-service --force-new-deployment` | **Automatic** on branch push. None. |
| **Runner** (`pmf_engine/runner/**`) | `build-pmf-engine.yml` pushes the `:pmf-engine-<env>` image; the Fargate task def points at that moving tag, so the next `RunTask` pulls it | **Automatic** (next run). None. |
| **Control-plane Lambdas** — dispatch, scheduler, task_reaper (`pmf_engine/control_plane/**`) | **Zip-packaged by Terraform** (`data.archive_file` over `pmf_engine/.lambda_build`). The image build does NOT touch them. | **Manual `terraform apply`.** A merge alone never updates them. |
| **Any `*.tf` change** (IAM, SG, task def cpu/mem/env, new resources) | Terraform state | **Manual `terraform apply`** for that module. |

**The trap:** you change `dispatch_handler.py` or `scheduler_handler.py`, merge to `develop`, see the green CI builds, and assume dev is deployed. It is not — those run in the zip Lambdas, which only update via `terraform apply`. (Runner-side files like `config.py`/`params.py` DO ride the image, so a change spanning both needs the image build AND the apply.)

## Deploy procedure (control-plane Lambdas / any terraform module)

Run from a checkout of the **target branch** — `archive_file` zips your local source, so a stale checkout ships stale Lambda code. Branch→env: `develop`→`dev`, `qa`→`qa`, `prod`→`prod`.

```bash
# 1. Be on the deployed branch's code
git fetch origin && git reset --hard origin/<branch>   # develop | qa | prod

# 2. Build the Lambda package (only needed for control-plane Lambda changes).
#    Re-zips dispatch_handler/scheduler_handler/task_reaper + vendored deps into
#    pmf_engine/.lambda_build, which the terraform archive_file points at.
bash pmf_engine/scripts/build_lambda_package.sh

# 3. Bridge AWS creds into Terraform (see "Credentials" below), then apply.
cd infrastructure/environments/<env>/<module>   # e.g. dev/pmf-engine-control-plane
eval "$(aws configure export-credentials --format env)"
terraform init -input=false
terraform plan -input=false -out=tfplan          # REVIEW before applying
terraform apply -input=false tfplan
```

**Always review the plan.** A clean control-plane code deploy is exactly
`0 to add, 3 to change, 0 to destroy` — the dispatch/scheduler/task_reaper
Lambdas with only `source_code_hash` (and `last_modified`) changing. If the plan
shows IAM/SG/S3/networking changes you did not intend, STOP — your checkout has
drift or other unapplied changes; surface it before applying.

Each environment is independent state (S3 backend key `<module>/<env>/terraform.tfstate`). Applying dev does not touch qa/prod. Promote through `develop`→`qa`→`prod`, applying each env from its own branch.

## Credentials

The README says `AWS_PROFILE=work`, but a typical local setup authenticates the
**`default`** profile via a login helper (`~/.aws/login/`). The `aws` CLI resolves
those creds, but Terraform's AWS provider does **not** read them and fails with
`No valid credential sources found` (then tries EC2 IMDS and times out).

Fix: bridge the CLI's live creds into the provider's env, in the **same shell** as
the terraform command (each invocation is a fresh shell):

```bash
eval "$(aws configure export-credentials --format env)"
```

No persistent env var is needed — do not hardcode keys in `.env`. Confirm the
target account first: `aws sts get-caller-identity` should show account `333022194791`.

## Quick reference

- Build script: `pmf_engine/scripts/build_lambda_package.sh` (writes `pmf_engine/.lambda_build`).
- Module dirs: `infrastructure/environments/<env>/<module>/`; modules in `infrastructure/modules/`.
- Verify image builds landed: `gh run list --workflow build-broker.yml --branch <branch>` (and `build-pmf-engine.yml`).
- Provider is region-only (no hardcoded `profile`), so default creds via the bridge above work.
