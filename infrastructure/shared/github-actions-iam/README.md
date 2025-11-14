# GitHub Actions IAM - OIDC Authentication

This Terraform module creates AWS IAM resources for GitHub Actions to authenticate and push Docker images to ECR without long-lived credentials.

## What This Creates

- **OIDC Provider**: GitHub Actions OIDC provider for AWS
- **IAM Role**: `github-actions-ecr-push` role for GitHub Actions workflows
- **IAM Policy**: ECR push permissions attached to the role

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
| IAM Role | `github-actions-ecr-push` | Assumable by GitHub Actions |
| IAM Policy | `ecr-push-policy` | Permissions to push to ECR |

## Permissions Granted

The role has permissions to:
- Get ECR authorization token
- Push Docker images to `gp-ai-projects` repository
- List/describe images in ECR

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
