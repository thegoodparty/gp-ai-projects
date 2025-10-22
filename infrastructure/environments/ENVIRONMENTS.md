# Environment Strategy: Dev → QA → Prod

## Overview

Three-tier environment strategy for progressive deployment and testing:

```
Dev (Development) → QA (Quality Assurance) → Prod (Production)
   ↓                      ↓                        ↓
Unstable             Stable Testing            Live Production
```

## Directory Structure

```
infrastructure/environments/
├── dev/
│   ├── serve-analyze-fargate/    ✅ DEPLOYED (V1 Pipeline)
│   └── shared-infra/             ✅ DEPLOYED (ALB + Route53)
│
├── qa/
│   ├── serve-analyze-fargate/    ✅ DEPLOYED (V1 Pipeline)
│   └── shared-infra/             ✅ DEPLOYED (ALB + Route53)
│
└── prod/
    ├── serve-analyze-fargate/    ✅ DEPLOYED (V1 Pipeline)
    └── shared-infra/             ✅ DEPLOYED (ALB + Route53)
```

## serve-analyze-fargate: Environment Comparison

| Configuration | Dev | QA | Prod |
|---------------|-----|----|----|
| **Terraform State** | `serve-analyze-fargate/dev/terraform.tfstate` | `serve-analyze-fargate/qa/terraform.tfstate` | `serve-analyze-fargate/prod/terraform.tfstate` |
| **Environment Variable** | `"dev"` | `"qa"` | `"prod"` |
| **VPC ID** | `vpc-01fed488c4047eaae` | Configurable (dev or prod VPC) | `vpc-0763fa52c32ebcf6a` |
| **Private Subnets** | 4 AZs | Configurable | 2 AZs |
| **Docker Image Tag** | `serve-analyze-dev` | `serve-analyze-qa` | `serve-analyze-prod` |
| **DynamoDB Table** | `serve-message-v1-dev` | `serve-message-v1-qa` | `serve-message-v1-prod` |
| **Purpose** | Active development | Pre-prod testing | Live production |
| **Stability** | Unstable | Stable | Very stable |
| **Deployment Frequency** | High (multiple/day) | Medium (daily/weekly) | Low (weekly/monthly) |
| **Cost Priority** | Minimize | Balance | Optimize |

## AWS Resources by Environment

### Resource Naming Pattern

All resources follow the pattern: `<service>-<component>-<environment>`

| Resource Type | Dev | QA | Prod |
|---------------|-----|----|----|
| **S3 Bucket** | `serve-analyze-data-dev` | `serve-analyze-data-qa` | `serve-analyze-data-prod` |
| **ECS Cluster** | `serve-analyze-dev` | `serve-analyze-qa` | `serve-analyze-prod` |
| **Lambda Function** | `serve-analyze-trigger-dev` | `serve-analyze-trigger-qa` | `serve-analyze-trigger-prod` |
| **Step Functions** | `serve-analyze-pipeline-dev` | `serve-analyze-pipeline-qa` | `serve-analyze-pipeline-prod` |
| **SNS Topic** | `serve-analyze-pipeline-failures-dev` | `serve-analyze-pipeline-failures-qa` | `serve-analyze-pipeline-failures-prod` |
| **CloudWatch Logs** | `/ecs/serve-analyze-dev` | `/ecs/serve-analyze-qa` | `/ecs/serve-analyze-prod` |
| **Security Group** | `serve-analyze-ecs-tasks-dev` | `serve-analyze-ecs-tasks-qa` | `serve-analyze-ecs-tasks-prod` |
| **IAM Roles** | `serve-analyze-*-dev` | `serve-analyze-*-qa` | `serve-analyze-*-prod` |

### ARN Examples

**Dev:**
```
Cluster:        arn:aws:ecs:us-west-2:333022194791:cluster/serve-analyze-dev
Lambda:         arn:aws:lambda:us-west-2:333022194791:function:serve-analyze-trigger-dev
Step Functions: arn:aws:states:us-west-2:333022194791:stateMachine:serve-analyze-pipeline-dev
SNS Topic:      arn:aws:sns:us-west-2:333022194791:serve-analyze-pipeline-failures-dev
```

**QA:**
```
Cluster:        arn:aws:ecs:us-west-2:333022194791:cluster/serve-analyze-qa
Lambda:         arn:aws:lambda:us-west-2:333022194791:function:serve-analyze-trigger-qa
Step Functions: arn:aws:states:us-west-2:333022194791:stateMachine:serve-analyze-pipeline-qa
SNS Topic:      arn:aws:sns:us-west-2:333022194791:serve-analyze-pipeline-failures-qa
```

**Prod:**
```
Cluster:        arn:aws:ecs:us-west-2:333022194791:cluster/serve-analyze-prod
Lambda:         arn:aws:lambda:us-west-2:333022194791:function:serve-analyze-trigger-prod
Step Functions: arn:aws:states:us-west-2:333022194791:stateMachine:serve-analyze-pipeline-prod
SNS Topic:      arn:aws:sns:us-west-2:333022194791:serve-analyze-pipeline-failures-prod
```

## Shared Resources

### ECR Repository (Single, Shared)
```
333022194791.dkr.ecr.us-west-2.amazonaws.com/gp-ai-projects
```

**Docker Image Tags:**
- `serve-analyze-dev:latest` - Dev environment
- `serve-analyze-qa:latest` - QA environment
- `serve-analyze-prod:latest` - Production environment

### Terraform State Bucket (Single, Shared)
```
goodparty-terraform-state-us-west-2
```

**State Keys:**
- `serve-analyze-fargate/dev/terraform.tfstate`
- `serve-analyze-fargate/qa/terraform.tfstate`
- `serve-analyze-fargate/prod/terraform.tfstate`

## API Endpoints

| Environment | ALB Endpoint | S3 Upload Path |
|-------------|--------------|----------------|
| **Dev** | `https://ai-dev.goodparty.org/serve/messages/process` | `s3://serve-analyze-data-dev/input/` |
| **QA** | `https://ai-qa.goodparty.org/serve/messages/process` | `s3://serve-analyze-data-qa/input/` |
| **Prod** | `https://ai.goodparty.org/serve/messages/process` | `s3://serve-analyze-data-prod/input/` |

## Deployment Flow

### Standard Promotion Path

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│     DEV     │────▶│     QA      │────▶│    PROD     │
│  (develop)  │     │   (test)    │     │   (live)    │
└─────────────┘     └─────────────┘     └─────────────┘
      ↓                    ↓                    ↓
  Unstable            Stable Test          Production
  Frequent            Integration          Controlled
  Changes             Testing              Releases
```

### Step-by-Step Process

1. **Dev Deployment**
   ```bash
   cd infrastructure/environments/dev/serve-analyze-fargate
   terraform apply
   # Test new features
   ```

2. **Build QA Image**
   ```bash
   # After dev testing passes
   docker tag serve-analyze-dev:latest serve-analyze-qa:latest
   docker push ...serve-analyze-qa
   ```

3. **QA Deployment**
   ```bash
   cd infrastructure/environments/qa/serve-analyze-fargate
   terraform apply
   # Run integration tests, UAT
   ```

4. **Build Prod Image**
   ```bash
   # After QA sign-off
   docker tag serve-analyze-qa:latest serve-analyze-prod:latest
   docker push ...serve-analyze-prod
   ```

5. **Prod Deployment**
   ```bash
   cd infrastructure/environments/prod/serve-analyze-fargate
   terraform apply
   # Monitor closely, run smoke tests
   ```

## Environment-Specific Considerations

### Development (Dev)

**Purpose**: Active feature development and debugging

**Characteristics:**
- ✅ Rapid iteration
- ✅ Breaking changes expected
- ✅ Debug logging enabled
- ✅ Lower resource limits
- ✅ Share VPC with QA for cost savings

**Testing Focus:**
- Unit testing
- Feature validation
- Bug reproduction
- API integration testing

**Deployment:**
- CI/CD from `develop` branch
- Multiple deployments per day
- No formal approval needed

### Quality Assurance (QA)

**Purpose**: Pre-production validation and testing

**Characteristics:**
- ✅ Stable codebase
- ✅ Production-like configuration
- ✅ Realistic test data
- ✅ Integration with external systems
- ✅ Can share dev or prod VPC

**Testing Focus:**
- Integration testing
- User acceptance testing (UAT)
- Performance testing
- Regression testing
- Security scanning

**Deployment:**
- Manual or scheduled from `develop` branch
- Daily or weekly deployments
- Requires dev team approval

### Production (Prod)

**Purpose**: Live customer-facing environment

**Characteristics:**
- ✅ Maximum stability
- ✅ Real customer data
- ✅ SLA requirements
- ✅ Full monitoring and alerting
- ✅ Dedicated VPC

**Testing Focus:**
- Smoke testing
- Canary deployments
- Monitoring and observability
- Incident response

**Deployment:**
- Manual from `main` branch
- Weekly or monthly releases
- Requires QA sign-off and product approval
- Change management process
- Rollback plan required

## Cost Estimation

| Environment | Monthly Cost (Estimated) | Notes |
|-------------|-------------------------|-------|
| **Dev** | $50-100 | Frequent testing, shared VPC |
| **QA** | $30-80 | Moderate testing, can share VPC |
| **Prod** | $100-500 | Production usage, dedicated resources |
| **Total** | $180-680 | Varies with usage patterns |

**Cost Optimization:**
- QA shares dev VPC (saves NAT gateway costs)
- Use Fargate Spot for dev/qa (up to 70% savings)
- S3 lifecycle policies archive old data
- CloudWatch log retention (7-30 days dev/qa, 90+ days prod)

## Monitoring and Alerts

### Alert Routing

| Environment | Severity | Destination |
|-------------|----------|-------------|
| **Dev** | Low | `#dev-alerts` Slack channel |
| **QA** | Medium | `#qa-alerts` Slack + email |
| **Prod** | High | `#prod-alerts` Slack + PagerDuty |

### CloudWatch Alarms

**Dev:**
- Basic error tracking
- Cost anomaly detection

**QA:**
- Full pipeline monitoring
- Performance degradation alerts

**Prod:**
- Real-time failure alerts
- SLA breach notifications
- Cost overrun alerts
- Security event monitoring

## Security Considerations

| Security Control | Dev | QA | Prod |
|------------------|-----|----|----|
| **VPC Isolation** | Shared OK | Configurable | Required |
| **API Key Rotation** | Manual | Quarterly | Monthly |
| **Data Encryption** | At rest | At rest + in transit | At rest + in transit + field-level |
| **Access Logging** | Basic | Full | Full + audit trail |
| **IAM Policies** | Permissive | Restrictive | Least privilege |
| **Secret Management** | Terraform vars | Terraform vars | AWS Secrets Manager recommended |

## Deployment Status

| Environment | Status | Infrastructure | Docker Image | Last Updated |
|-------------|--------|---------------|--------------|--------------|
| **Dev** | ✅ Live | Fully deployed | `serve-analyze-dev` | Active |
| **QA** | ✅ Live | Fully deployed | `serve-analyze-qa` | Oct 21, 2025 |
| **Prod** | ✅ Live | Fully deployed | `serve-analyze-prod` | Active |

## Deployment History

### Dev
- ✅ Infrastructure deployed and active
- ✅ Docker image: `serve-analyze-dev` (latest)
- ✅ ALB: `ai-dev.goodparty.org`
- ✅ S3 bucket: `serve-analyze-data-dev`

### QA
- ✅ Infrastructure deployed and active (Oct 2025)
- ✅ Docker image: `serve-analyze-qa` (Oct 21, 2025)
- ✅ ALB: `ai-qa.goodparty.org`
- ✅ S3 bucket: `serve-analyze-data-qa`
- ✅ ECS Cluster: `serve-analyze-qa` (ACTIVE, 0 running tasks)

### Prod
- ✅ Infrastructure deployed and active
- ✅ Docker image: `serve-analyze-prod` (latest)
- ✅ ALB: `ai.goodparty.org`
- ✅ S3 bucket: `serve-analyze-data-prod`

## Quick Reference Commands

### Build and Push Images

```bash
# Dev
docker buildx build --platform linux/arm64 -t serve-analyze-dev -f serve/v1_pipeline/Dockerfile .
docker push 333022194791.dkr.ecr.us-west-2.amazonaws.com/gp-ai-projects:serve-analyze-dev

# QA
docker tag serve-analyze-dev:latest serve-analyze-qa:latest
docker push 333022194791.dkr.ecr.us-west-2.amazonaws.com/gp-ai-projects:serve-analyze-qa

# Prod
docker tag serve-analyze-qa:latest serve-analyze-prod:latest
docker push 333022194791.dkr.ecr.us-west-2.amazonaws.com/gp-ai-projects:serve-analyze-prod
```

### Deploy Infrastructure

```bash
# Dev
cd infrastructure/environments/dev/serve-analyze-fargate
AWS_PROFILE=work terraform apply

# QA
cd infrastructure/environments/qa/serve-analyze-fargate
AWS_PROFILE=work terraform apply

# Prod
cd infrastructure/environments/prod/serve-analyze-fargate
AWS_PROFILE=work terraform apply
```

### Trigger Pipelines

```bash
# Dev
aws s3 cp test.csv s3://serve-analyze-data-dev/input/

# QA
aws s3 cp test.csv s3://serve-analyze-data-qa/input/

# Prod
aws s3 cp test.csv s3://serve-analyze-data-prod/input/
```

### Monitor Executions

```bash
# Dev
aws stepfunctions list-executions \
  --state-machine-arn arn:aws:states:us-west-2:333022194791:stateMachine:serve-analyze-pipeline-dev

# QA
aws stepfunctions list-executions \
  --state-machine-arn arn:aws:states:us-west-2:333022194791:stateMachine:serve-analyze-pipeline-qa

# Prod
aws stepfunctions list-executions \
  --state-machine-arn arn:aws:states:us-west-2:333022194791:stateMachine:serve-analyze-pipeline-prod
```

## Support and Documentation

- **Dev README**: `infrastructure/environments/dev/serve-analyze-fargate/README.md`
- **QA README**: `infrastructure/environments/qa/serve-analyze-fargate/README.md`
- **Prod README**: `infrastructure/environments/prod/serve-analyze-fargate/README.md`
- **Module Documentation**: `infrastructure/modules/serve-analyze-fargate/README.md`
