# GitHub Actions Workflows

⚠️ **STATUS: NOT ACTIVE YET**

The workflows are currently **disabled** and located in `.github/workflows-disabled/`. They are ready for testing to be added when we have time.

## Available Workflows (Currently Disabled)

### 1. Lint and Type Check (`lint-and-type-check.yml`)
Runs ruff linting, formatting checks, and mypy type checking on PRs and pushes.

### 2. Serve Analyze Build Pipeline (`build-serve-analyze.yml`)
Automatically builds and pushes Docker images to ECR whenever code changes are pushed to specific branches or paths.

---

## Activation Instructions

When ready to enable GitHub Actions:

1. **Move workflows to active directory:**
   ```bash
   mv .github/workflows-disabled/*.yml .github/workflows/
   ```

2. **Verify IAM role exists:**
   ```bash
   AWS_PROFILE=work aws iam get-role --role-name github-actions-ecr-push
   ```

3. **Push to trigger first build:**
   ```bash
   git push origin develop
   ```

---

## Serve Analyze Build Pipeline (Details)

### Overview

The `build-serve-analyze.yml` workflow automatically builds and pushes Docker images to ECR whenever code changes are pushed to specific branches or paths.

### Trigger Conditions

**Automatic triggers:**
- Push to `main`, `develop`, or `serve` branches
- New version tags (e.g., `v1.2.3`)
- Changes to these paths:
  - `serve/v1_pipeline/**`
  - `serve/hierarchical_discovery/**`
  - `shared/**`
  - `pyproject.toml`
  - `uv.lock`

**Manual trigger:**
- Use GitHub Actions UI to run with custom environment (dev/prod)

### Tagging Strategy

| Branch/Tag | ECR Tag | Environment |
|------------|---------|-------------|
| `develop` | `serve-analyze-dev` | dev |
| `main` | `serve-analyze-main` | prod |
| `v1.2.3` | `serve-analyze-v1.2.3` | prod |
| Other | `serve-analyze-<git-sha>` | dev |

All builds also tag `v1-pipeline-latest` for easy reference.

### Setup Instructions

#### 1. Deploy AWS IAM Role (One-time)

```bash
cd infrastructure

# Add the github-actions-iam.tf to your terraform
# Update the repo name in the assume role policy:
# "repo:YOUR-ORG/YOUR-REPO:*"

terraform init
terraform plan
terraform apply
```

This creates:
- OIDC provider for GitHub Actions
- IAM role: `github-actions-ecr-push`
- Permissions to push to ECR

#### 2. Configure GitHub Repository

No secrets needed! The workflow uses OIDC (OpenID Connect) to authenticate to AWS.

**Required:** The workflow must use the role ARN from the terraform output:
```
arn:aws:iam::333022194791:role/github-actions-ecr-push
```

#### 3. Test the Workflow

**Option A: Push to develop branch**
```bash
git checkout develop
git push origin develop
```

**Option B: Manual trigger**
1. Go to Actions tab in GitHub
2. Select "Build and Push Serve Analyze"
3. Click "Run workflow"
4. Choose environment (dev/prod)
5. Click "Run workflow"

### Build Performance

**GitHub Actions runners:**
- Pre-configured with Docker Buildx
- Multi-platform build support
- Build cache using GitHub Actions cache
- Typical build time: 5-10 minutes

**Cache optimization:**
```yaml
cache-from: type=gha
cache-to: type=gha,mode=max
```
- First build: ~10 minutes
- Subsequent builds with cache: ~2-3 minutes

### Monitoring

**View build logs:**
1. Go to Actions tab
2. Click on workflow run
3. Expand "Build and push Docker image" step

**Verify image in ECR:**
```bash
AWS_PROFILE=work aws ecr describe-images \
  --repository-name gp-ai-projects \
  --query 'imageDetails[?contains(imageTags, `serve-analyze`)]' \
  --output table
```

### Advantages Over Local Builds

✅ **Consistent environment** - Same build every time
✅ **No local resources** - Runs on GitHub's infrastructure
✅ **Automatic on push** - No manual commands
✅ **Build cache** - Faster subsequent builds
✅ **No AWS credentials** - Uses OIDC authentication
✅ **Multi-platform** - Can build for different architectures

### Troubleshooting

**Build fails with "role cannot be assumed":**
- Check IAM role exists: `github-actions-ecr-push`
- Verify repo name in assume role policy matches your repo
- Confirm OIDC provider thumbprint is correct

**Build fails with "access denied to ECR":**
- Check IAM role has ECR push permissions
- Verify ECR repository exists: `gp-ai-projects`

**Build is slow:**
- First build will be slow (no cache)
- Subsequent builds should be faster with cache
- Check if cache is being used in logs

### Manual Build Comparison

**Local:**
```bash
cd serve/v1_pipeline
AWS_PROFILE=work PUSH_TO_ECR=true ./build.sh dev
```

**GitHub Actions:**
```bash
git commit -m "Update code"
git push origin develop
# Build happens automatically
```

### Future Enhancements

- [ ] Add automatic ECS task definition updates
- [ ] Slack notifications on build success/failure
- [ ] Deploy to dev environment automatically
- [ ] Run tests before building
- [ ] Multi-stage deployments (dev → staging → prod)
