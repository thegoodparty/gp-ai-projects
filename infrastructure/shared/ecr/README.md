# Shared ECR Repository for AI Projects

Single ECR repository for all gp-ai-projects Docker images.

## Repository

**Name**: `gp-ai-projects`
**URL**: `333022194791.dkr.ecr.us-west-2.amazonaws.com/gp-ai-projects`
**Region**: us-west-2
**ARN**: `arn:aws:ecr:us-west-2:333022194791:repository/gp-ai-projects`

**Images stored**:
- `serve-analyze-dev` - V1 Pipeline (dev environment)
- `serve-analyze-qa` - V1 Pipeline (qa environment)
- `serve-analyze-prod` - V1 Pipeline (prod environment)
- Future AI project images...

## Deploy ECR Repository

```bash
cd infrastructure/shared/ecr

terraform init
terraform plan
terraform apply
```

## Build and Push Images

### serve-analyze (V1 Pipeline)

```bash
# Get ECR repository URL
ECR_REPO=333022194791.dkr.ecr.us-west-2.amazonaws.com/gp-ai-projects

# Build for ARM64 (Fargate ARM64 is cheaper)
cd serve/v1_pipeline
docker buildx build --platform linux/arm64 -t serve-analyze-dev -f Dockerfile ../..

# Tag for specific environment
docker tag serve-analyze-dev:latest ${ECR_REPO}:serve-analyze-dev

# Login to ECR
aws ecr get-login-password --region us-west-2 --profile work | \
  docker login --username AWS --password-stdin ${ECR_REPO}

# Push to ECR
docker push ${ECR_REPO}:serve-analyze-dev
```

### Build for All Environments

```bash
ECR_REPO=333022194791.dkr.ecr.us-west-2.amazonaws.com/gp-ai-projects
cd serve/v1_pipeline

# Build once
docker buildx build --platform linux/arm64 -t serve-analyze -f Dockerfile ../..

# Tag for each environment
docker tag serve-analyze:latest ${ECR_REPO}:serve-analyze-dev
docker tag serve-analyze:latest ${ECR_REPO}:serve-analyze-qa
docker tag serve-analyze:latest ${ECR_REPO}:serve-analyze-prod

# Login
aws ecr get-login-password --region us-west-2 --profile work | \
  docker login --username AWS --password-stdin ${ECR_REPO}

# Push all
docker push ${ECR_REPO}:serve-analyze-dev
docker push ${ECR_REPO}:serve-analyze-qa
docker push ${ECR_REPO}:serve-analyze-prod
```

## Lifecycle Policy

Convention-based retention policy to prevent active projects from evicting stable ones:

**Priority 1**: Environment images - **Never expire** ✅
- Tags: `main`, `master`, `prod`, `dev`, `release`
- Example: `main`, `dev`, `prod`, `v1-pipeline-prod`, `campaign-planner-release`

**Priority 2**: Versioned releases - **365 days**
- Tags: `v1.*`, `v2.*`, `v3.*`, etc.
- Example: `v1.0.0`, `v2.1.3`, `campaign-planner-v1.5.2`

**Priority 3**: serve-analyze images - **180 days** (stable project)
- Tags: `serve-analyze-*`

**Priority 4**: campaign-planner images - **90 days** (active development)
- Tags: `campaign-planner-*`

**Priority 5**: Development tags - **60 days**
- Tags: `latest`, `staging` (note: `dev` never expires, see Priority 1)

**Priority 6**: Untagged images - **90 days**
- Previous builds when tag moves (e.g., old "latest" builds)
- Intermediate/dangling layers

**Priority 7**: Catch-all - **30 days**
- Any other tags not matching above rules

**Key Benefits:**
- 🔒 Environment images (`main`, `master`, `prod`, `dev`, `release`) **never expire**
- 📦 Versioned releases kept for 1 year
- 🎯 Per-project retention (stable vs active development)
- 🔄 Previous "latest" builds kept for 90 days even when tag moves
- 🧹 Automatic cleanup of old/untagged images

**Important Note on "latest" Tags:**
When you push a new image with tag `v1-pipeline-latest`, the tag moves to the new image and the previous image becomes **untagged**. We keep untagged images for 90 days so you can roll back if needed.

Example:
```
Day 1:  Image A tagged "v1-pipeline-latest"
Day 10: Push Image B with "v1-pipeline-latest"
        → Tag moves to Image B
        → Image A becomes untagged but kept for 90 days
Day 100: Image A deleted (90 days after becoming untagged)
```

## Usage in Terraform

```hcl
data "terraform_remote_state" "shared_ecr" {
  backend = "s3"
  config = {
    bucket = "gp-terraform-state"
    key    = "shared/ecr/terraform.tfstate"
    region = "us-east-1"
  }
}

resource "aws_ecs_task_definition" "example" {
  container_definitions = jsonencode([{
    name  = "my-container"
    image = "${data.terraform_remote_state.shared_ecr.outputs.repository_url}:v1-pipeline-latest"
  }])
}
```

## Tagging Strategy

### Basic Tag Formats

**Environment Tags (Never Expire):**
- `main` - Main branch build (any project)
- `master` - Master branch build
- `prod` - Production deployment
- `dev` - Development deployment
- `release` - Release build

**Versioned Releases (365 days):**
- `v1.0.0`, `v2.1.3` - Semantic version tags

**Project-Specific:**
- `{project}-{environment}` - Standard project tagging
- `{project}-{version}` - Project versioned releases

**Examples:**
- `main` ← **Never expires** (environment tag)
- `dev` ← **Never expires** (environment tag)
- `prod` ← **Never expires** (environment tag)
- `serve-analyze-prod` ← **Never expires** (contains "prod")
- `campaign-planner-release` ← **Never expires** (contains "release")
- `v1.5.2` ← 365 days (versioned release)
- `serve-analyze-latest` ← 180 days (project-specific)
- `campaign-planner-dev` ← 90 days (project-specific, but `dev` alone never expires)
- `staging` ← 60 days (dev tag)

### Moving Tag Problem

**⚠️ Issue with "latest" Tags:**

When you push a new image with the same tag (e.g., `serve-analyze-latest`), the tag **moves** to the new image:

```
Day 1:  Image A tagged "serve-analyze-latest"
Day 10: Push Image B with "serve-analyze-latest"
        → Tag moves to Image B
        → Image A becomes UNTAGGED
        → Image A deleted after 90 days (untagged retention)
```

### Recommended Tagging Strategies

**Strategy 1: Timestamp Tags (Recommended for Development)**

Keep every build tagged with a unique timestamp:

```bash
# Build with timestamp
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
ECR_REPO=333022194791.dkr.ecr.us-west-2.amazonaws.com/gp-ai-projects
docker buildx build --platform linux/arm64 -t serve-analyze-dev-${TIMESTAMP} ../..
docker push ${ECR_REPO}:serve-analyze-dev-${TIMESTAMP}
# → Tag: serve-analyze-dev-20251010-143022 (180 days retention)

# Git commit SHA (best for traceability)
GIT_SHA=$(git rev-parse --short HEAD)
docker buildx build --platform linux/arm64 -t serve-analyze-dev-${GIT_SHA} ../..
docker push ${ECR_REPO}:serve-analyze-dev-${GIT_SHA}
# → Tag: serve-analyze-dev-abc1234 (180 days retention)
```

**Benefits:**
- ✅ Every build stays tagged (180-day retention, not 90-day untagged)
- ✅ Easy to identify builds by timestamp/commit
- ✅ Full rollback capability for 6 months
- ✅ No tag collisions

**Strategy 2: Moving Tags + Versioned Releases**

Use moving tags for environments, versioned tags for releases:

```bash
ECR_REPO=333022194791.dkr.ecr.us-west-2.amazonaws.com/gp-ai-projects

# Development (tag moves)
docker buildx build --platform linux/arm64 -t serve-analyze-dev ../..
docker push ${ECR_REPO}:serve-analyze-dev
# → Tag: serve-analyze-dev (moves with each push)

# Staging (tag moves)
docker buildx build --platform linux/arm64 -t staging ../..
docker push ${ECR_REPO}:staging
# → Tag: staging (moves with each push)

# Production releases (unique tags)
docker buildx build --platform linux/arm64 -t v1.2.3 ../..
docker push ${ECR_REPO}:v1.2.3
# → Tag: v1.2.3 (never moves, 365 days)

docker push ${ECR_REPO}:serve-analyze-prod
# → Tag: serve-analyze-prod (never expires)
```

**Benefits:**
- ✅ Clear environment indicators (dev, staging, prod)
- ✅ Versioned releases never expire
- ⚠️ Previous dev/staging builds become untagged (90-day retention)

**Strategy 3: Hybrid Approach (Best of Both Worlds)**

Combine moving environment tags with timestamped tags:

```bash
# Development: Use timestamp + also tag as "dev"
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
GIT_SHA=$(git rev-parse --short HEAD)

# Build and tag strategy
ECR_REPO=333022194791.dkr.ecr.us-west-2.amazonaws.com/gp-ai-projects

# Build once
docker buildx build --platform linux/arm64 -t serve-analyze ../..

# Timestamped tag (keeps build tagged for 180 days)
docker tag serve-analyze:latest ${ECR_REPO}:serve-analyze-dev-${GIT_SHA}
docker push ${ECR_REPO}:serve-analyze-dev-${GIT_SHA}

# Environment tag (moves with each push)
docker tag serve-analyze:latest ${ECR_REPO}:serve-analyze-dev
docker push ${ECR_REPO}:serve-analyze-dev

# Production releases
docker tag serve-analyze:latest ${ECR_REPO}:v1.2.3
docker tag serve-analyze:latest ${ECR_REPO}:serve-analyze-prod
docker push ${ECR_REPO}:v1.2.3
docker push ${ECR_REPO}:serve-analyze-prod
```

**Benefits:**
- ✅ Environment tags for easy deployment (Fargate pulls `serve-analyze-dev`)
- ✅ Timestamped tags for rollback capability
- ✅ Versioned releases for production
- ✅ Best of all approaches

### Tagging Rules Summary

| Tag Pattern | Retention | Use Case | Tag Moves? |
|-------------|-----------|----------|------------|
| `main`, `master`, `prod`, `dev`, `release` | **Never** | Environment deployments | Yes (but never expires) |
| `v1.0.0`, `v2.1.3` | **365 days** | Versioned releases | No |
| `{project}-*` | **180 days** (serve-analyze)<br>**90 days** (campaign-planner) | Project-specific builds | Depends on strategy |
| `latest`, `staging` | **60 days** (tagged)<br>**90 days** (untagged) | Development/testing | Yes (becomes untagged) |
| Untagged images | **90 days** | Previous "latest" builds | N/A |
| Other tags | **30 days** | Temporary/experimental | Depends |

### Best Practices

**1. Environment Deployments:**
```bash
# Never-expiring environment tags
PUSH_TO_ECR=true ./build.sh dev           # Never expires
PUSH_TO_ECR=true ./build.sh prod          # Never expires
PUSH_TO_ECR=true ./build.sh main          # Never expires

# Versioned releases
PUSH_TO_ECR=true ./build.sh v1.2.3        # 365 days
```

**2. Development Builds (with history):**
```bash
ECR_REPO=333022194791.dkr.ecr.us-west-2.amazonaws.com/gp-ai-projects

# Use timestamps to keep all builds tagged
GIT_SHA=$(git rev-parse --short HEAD)
docker buildx build --platform linux/arm64 -t serve-analyze-dev-${GIT_SHA} ../..
docker push ${ECR_REPO}:serve-analyze-dev-${GIT_SHA}  # 180 days, never becomes untagged

# Or just use "dev" if you only need current deployment
docker push ${ECR_REPO}:serve-analyze-dev           # Never expires (tag moves but never deleted)
```

**3. Rollback Safety:**
- Use versioned tags (`v1.2.3`) for releases you may need to rollback to
- Use timestamped tags (`serve-analyze-dev-20251010-143022`) to keep full build history
- Avoid relying on untagged image retention (90 days) for critical builds

**4. Tag Naming:**
- Include project name for multi-project repositories: `serve-analyze-dev-abc1234`
- Include git commit SHA for traceability: `campaign-planner-v1.2.3-abc1234`
- Use semantic versioning for releases: `v1.2.3`, `v2.0.0`

## Outputs

- `repository_url` - Full ECR repository URL
- `repository_name` - Repository name (gp-ai-projects)
- `repository_arn` - ARN for IAM policies
