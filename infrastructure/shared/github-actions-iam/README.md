# GitHub Actions IAM - OIDC Authentication

This Terraform module creates AWS IAM resources for GitHub Actions to authenticate to AWS without long-lived credentials.

## What This Creates

- **OIDC Provider**: GitHub Actions OIDC provider for AWS
- **IAM Role**: `github-actions-ecr-push` — ECR image push (assumable from any ref in the repo) plus broker ECS redeploys
- **IAM Role**: `github-actions-lambda-deploy` — Lambda code deploys (`clickup-bot-*`, `campaign-plan-*`), assumable ONLY from the `develop`, `qa`, and `prod` branches
- **IAM Policies**: ECR push / ECS deploy permissions on the ecr-push role, Lambda `UpdateFunctionCode` on the lambda-deploy role

## Rollout ordering — lambda deploy role (read before applying)

Nothing in CI applies this stack; it is applied manually. Two orderings matter
when a change moves workflows onto `github-actions-lambda-deploy`:

1. **Apply BEFORE merging workflow changes that reference the role.** The
   deploy workflows (`deploy-clickup-bot.yml`, `deploy-campaign-plan-lambda.yml`)
   assume `arn:aws:iam::333022194791:role/github-actions-lambda-deploy` at the
   configure-aws-credentials step. If the role does not exist yet, the first
   push that touches those workflows' paths fails with
   `Not authorized to perform sts:AssumeRoleWithWebIdentity` and the Lambda
   code deploy is stranded. Run `terraform apply` here first, then merge.
2. **Promote the workflow change through develop, qa, and prod immediately
   after the apply.** The apply that creates the new role also detaches
   `lambda-deploy-policy` from `github-actions-ecr-push` (the policy resource
   moved roles, which is ForceNew). GitHub Actions runs the workflow file at
   the triggering ref, so until the new workflow files land on each long-lived
   branch, that branch still runs the OLD workflow that assumes the ecr-push
   role — its `update-function-code` call fails with AccessDenied and the
   deploy silently does not ship. `deploy-campaign-plan-lambda.yml` deploys
   from all three branches (`develop` → `campaign-plan-dev`, `qa`, `prod`), so
   `develop` is affected too, not just qa/prod — its campaign-plan deploys stay
   broken until this change lands on `develop`. (`deploy-clickup-bot.yml`
   deploys from `prod` only, so it is unaffected on develop/qa.) Keep the
   window short: apply, then merge/promote through develop → qa → prod in the
   same sitting, and watch the first run of each deploy workflow succeed.

## Authentication Flow

```
GitHub Actions Workflow
  ↓
OIDC Token (from GitHub)
  ↓
AWS STS AssumeRoleWithWebIdentity
  ↓
Temporary AWS Credentials
  ↓
Push to ECR (gp-ai-projects repository)
```

## Security

The IAM role can **only** be assumed by:
- Workflows from the repository: `thegoodparty/gp-ai-projects`
- Using GitHub's OIDC provider

No long-lived AWS credentials are stored in GitHub!

## Deployment

**One-time setup** (already configured in this module):

```bash
cd infrastructure/shared/github-actions-iam

# Initialize Terraform
AWS_PROFILE=work terraform init

# Review changes
AWS_PROFILE=work terraform plan

# Deploy
AWS_PROFILE=work terraform apply
```

## Verification

After deployment, verify the role exists:

```bash
AWS_PROFILE=work aws iam get-role --role-name github-actions-ecr-push
```

Expected output shows the role ARN:
```
arn:aws:iam::333022194791:role/github-actions-ecr-push
```

## Usage in GitHub Actions

The role ARN is referenced in `.github/workflows/build-serve-analyze.yml`:

```yaml
- name: Configure AWS credentials
  uses: aws-actions/configure-aws-credentials@v4
  with:
    role-to-assume: arn:aws:iam::333022194791:role/github-actions-ecr-push
    aws-region: us-west-2
```

No secrets needed in GitHub repository settings!

## Terraform State

- **Bucket**: `goodparty-terraform-state-us-west-2`
- **Key**: `shared/github-actions-iam/terraform.tfstate`
- **Region**: us-west-2

## Resources Created

| Resource Type | Name | Purpose |
|---------------|------|---------|
| OIDC Provider | `token.actions.githubusercontent.com` | Trust relationship with GitHub |
| IAM Role | `github-actions-ecr-push` | ECR push + broker ECS deploy, any ref |
| IAM Role | `github-actions-lambda-deploy` | Lambda code deploys, develop/qa/prod refs only |
| IAM Policy | `ecr-push-policy` | Permissions to push to ECR |
| IAM Policy | `ecs-deploy-policy` | broker ECS force-new-deployment |
| IAM Policy | `lambda-deploy-policy` | `lambda:UpdateFunctionCode` on `clickup-bot-*`, `campaign-plan-*` |

## Permissions Granted

**`github-actions-ecr-push`** (assumable from any ref):
- `ecr:GetAuthorizationToken` (account-wide — required by the ECR login step)
- Push/pull layers and images on the `gp-ai-projects` ECR repository
  (`InitiateLayerUpload`, `UploadLayerPart`, `CompleteLayerUpload`, `PutImage`,
  `BatchCheckLayerAvailability`, `GetDownloadUrlForLayer`, `BatchGetImage`, plus
  `DescribeRepositories` / `ListImages` / `DescribeImages` / `GetRepositoryPolicy`)
- `ecs:UpdateService` / `ecs:DescribeServices` on the `broker-{dev,qa,prod}` services
  (force-new-deployment from the build-broker workflow)

**`github-actions-lambda-deploy`** (assumable only from `develop` / `qa` / `prod`):
- `lambda:UpdateFunctionCode`, `lambda:GetFunction`, `lambda:GetFunctionConfiguration`,
  scoped to `clickup-bot-*` and `campaign-plan-*` functions only — never `function:*`,
  so CI cannot overwrite the other prod Lambdas in this account.

## Troubleshooting

**Role already exists error:**
- The role may have been created manually
- Import it: `terraform import aws_iam_role.github_actions_ecr_push github-actions-ecr-push`

**OIDC provider already exists:**
- Import it: `terraform import aws_iam_openid_connect_provider.github_actions <provider-arn>`
- Find ARN: `aws iam list-open-id-connect-providers`

**Workflow can't assume role:**
- Verify repository name in trust policy matches your repo
- Check OIDC provider thumbprint is correct
- Ensure workflow has `id-token: write` permission

## Related Documentation

- [GitHub Actions OIDC Guide](https://docs.github.com/en/actions/deployment/security-hardening-your-deployments/configuring-openid-connect-in-amazon-web-services)
- [AWS IAM OIDC Providers](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_providers_create_oidc.html)
