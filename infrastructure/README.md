# Infrastructure

AWS infrastructure for the GoodParty.org AI platform using **ECS Fargate** for compute with **Application Load Balancer** for HTTP triggers and **S3 Events** for automated processing.

## Architecture Philosophy

This infrastructure is designed for **scalable AI pipeline processing**:
- ECS Fargate tasks for message classification, clustering, and analysis
- Dual trigger modes: HTTP API (via ALB) and S3 file uploads
- Serverless cost model: Pay only when pipelines run
- Production-grade monitoring with failure alerts via SNS/Slack
- Shared infrastructure (ALB, ECR, Route53) across all AI services

## Shared Resources

### ECR Repository
- **Name**: `gp-ai-projects`
- **URL**: `333022194791.dkr.ecr.us-west-2.amazonaws.com/gp-ai-projects`
- **Region**: us-west-2
- **Purpose**: Shared Docker image repository for all AI projects
- **Terraform**: `infrastructure/shared/ecr/`

### Terraform State Bucket
- **Name**: `goodparty-terraform-state-us-west-2`
- **Region**: us-west-2
- **Features**: Versioning enabled, encryption enabled, public access blocked
- **Purpose**: Centralized Terraform state storage for all GoodParty infrastructure

### AWS Account
- **Account ID**: `333022194791`
- **Primary Region**: us-west-2
- **Profile**: `work` (in `~/.aws/config`)

## Directory Structure

```
infrastructure/
├── environments/                    # Environment-specific deployments
│   ├── dev/
│   │   ├── serve-analyze-fargate/  # Dev pipeline deployment
│   │   └── shared-infra/           # Dev ALB + Route53
│   ├── qa/
│   │   ├── serve-analyze-fargate/  # QA pipeline deployment
│   │   └── shared-infra/           # QA ALB + Route53
│   └── prod/
│       ├── serve-analyze-fargate/  # Prod pipeline deployment
│       └── shared-infra/           # Prod ALB + Route53
│
├── modules/                        # Reusable Terraform modules
│   ├── serve-analyze-fargate/      # ECS Fargate pipeline module
│   │   ├── main.tf                 # ECS cluster, task definition, Lambda trigger
│   │   ├── lambda-trigger/         # TypeScript Lambda for ALB/S3 triggers
│   │   └── slack-notifier/         # Lambda for Slack failure alerts
│   ├── alb/                        # Application Load Balancer module
│   │   ├── main.tf
│   │   ├── variables.tf
│   │   └── outputs.tf
│   └── route53/                    # Route53 DNS module
│       ├── main.tf
│       ├── variables.tf
│       └── outputs.tf
│
├── shared/                         # Shared resources (deploy once)
│   ├── ecr/                        # Docker image repository
│   │   ├── main.tf                 # ECR repository + lifecycle policy
│   │   └── README.md               # ECR documentation
│   └── slack-notifier/             # Shared Slack notifier Lambda
│       └── main.tf                 # Lambda for failure alerts
│
├── deploy.sh                       # Deployment script
└── README.md                       # This file
```

## Architecture Overview

### Dual-Trigger ECS Fargate Architecture

```
                      Trigger Options
                           ↓
        ┌──────────────────┴──────────────────┐
        │                                      │
    HTTP POST                              S3 Upload
  (via ALB)                               (CSV file)
        │                                      │
        ↓                                      ↓
   Route53 DNS                           S3 Event
        ↓                                      ↓
Application Load Balancer              S3 Notification
        ↓                                      ↓
  x-api-key auth                        Lambda Permission
        ↓                                      │
        └──────────────┬───────────────────────┘
                       ↓
            Lambda Trigger Function
           (TypeScript, dual-mode handler)
                       ↓
          Starts ECS Fargate Task
          (4 vCPU, 16 GB RAM)
                       ↓
        Pipeline Container Executes:
        - Message consolidation
        - AI classification (Gemini)
        - Multi-cluster analysis
        - DynamoDB upload (via API)
                       ↓
         Results uploaded to S3
         (output/ directory)
                       ↓
          EventBridge monitors task
                       ↓
        Failure alerts → SNS → Slack
```

**Current Services:**
- ✅ `/serve/messages/process` - V1 pipeline HTTP trigger (production)
- ✅ S3 input uploads - Automatic pipeline execution (production)
- 🔜 `/ml/*` - Machine learning inference (planned)
- 🔜 `/analytics/*` - Data analytics API (planned)

### Infrastructure Layers

**Shared Resources (Deploy Once):**
- **ECR Repository**: Docker images for all AI projects (`gp-ai-projects`)
- **Application Load Balancer**: HTTPS endpoint with x-api-key auth
- **Route53**: DNS records (ai-dev.goodparty.org, ai.goodparty.org)
- **Slack Notifier**: Failure alert Lambda (shared across environments)

**Per-Environment Resources:**
- **ECS Cluster**: Fargate cluster for pipeline tasks
- **S3 Bucket**: Input/output data storage
- **Lambda Trigger**: Handles ALB and S3 events
- **SNS Topic**: Pipeline failure notifications
- **EventBridge Rules**: Task failure monitoring
- **IAM Roles**: ECS execution and task roles
- **Security Groups**: ECS task network access

## Deployment Workflow

### Prerequisites
- AWS CLI configured with `work` profile
- Terraform 1.0+ installed
- Docker for building pipeline images
- Node.js 22+ for Lambda builds
- Access to `goodparty.org` hosted zone and `*.goodparty.org` certificate
- AWS Secrets Manager secrets: `AI_SECRETS_DEV`, `AI_SECRETS_PROD`

### Complete Deployment (First Time)

#### Step 1: Deploy Shared ECR Repository
```bash
cd infrastructure/shared/ecr
AWS_PROFILE=work terraform init
AWS_PROFILE=work terraform apply
```

**What this creates:**
- ECR Repository: `gp-ai-projects` (333022194791.dkr.ecr.us-west-2.amazonaws.com/gp-ai-projects)

#### Step 2: Build and Push Docker Image
```bash
cd serve/v1_pipeline

# Build for ARM64 (Fargate ARM64 is cheaper)
docker buildx build --platform linux/arm64 -t serve-analyze-dev -f Dockerfile ../..

# Tag for ECR
ECR_REPO=333022194791.dkr.ecr.us-west-2.amazonaws.com/gp-ai-projects
docker tag serve-analyze-dev:latest ${ECR_REPO}:serve-analyze-dev

# Login and push
aws ecr get-login-password --region us-west-2 --profile work | \
  docker login --username AWS --password-stdin ${ECR_REPO}
docker push ${ECR_REPO}:serve-analyze-dev
```

#### Step 3: Deploy serve-analyze-fargate
```bash
cd infrastructure/environments/dev/serve-analyze-fargate

# Create terraform.tfvars
cat > terraform.tfvars <<EOF
vpc_id             = "vpc-01fed488c4047eaae"
private_subnet_ids = ["subnet-xxx", "subnet-yyy"]
failure_notification_email = "team@example.com"
EOF

AWS_PROFILE=work terraform init
AWS_PROFILE=work terraform apply
```

**What this creates:**
- ECS Cluster: `serve-analyze-dev`
- S3 Bucket: `serve-analyze-data-dev`
- Lambda Function: `serve-analyze-trigger-dev`
- SNS Topic: `serve-analyze-pipeline-failures-dev`
- EventBridge Rules: Task failure monitoring

#### Step 4: Deploy Shared Infrastructure (ALB + Route53)
```bash
cd infrastructure/environments/dev/shared-infra

# Create terraform.tfvars
cat > terraform.tfvars <<EOF
aws_region         = "us-west-2"
environment        = "dev"
vpc_id             = "vpc-01fed488c4047eaae"
public_subnet_ids  = ["subnet-aaa", "subnet-bbb"]
certificate_arn    = "arn:aws:acm:us-west-2:333022194791:certificate/xxx"
route53_zone_id    = "Z123456789ABC"
custom_domain_name = "ai-dev.goodparty.org"
EOF

AWS_PROFILE=work terraform init
AWS_PROFILE=work terraform apply
```

**What this creates:**
- ALB: `ai-dev` load balancer
- Target Group: `serve-analyze-dev`
- Listener Rules: `/serve/messages/process` with x-api-key auth
- Route53 Record: `ai-dev.goodparty.org` → ALB

#### Step 5: Verify Deployment

**Test HTTP Trigger:**
```bash
curl -X POST https://ai-dev.goodparty.org/serve/messages/process \
  -H "Content-Type: application/json" \
  -H "x-api-key: YOUR_API_KEY" \
  -d '{
    "csvS3Path": "s3://serve-analyze-data-dev/input/test.csv"
  }'
```

**Test S3 Trigger:**
```bash
# Upload CSV file
aws s3 cp test-data.csv s3://serve-analyze-data-dev/input/test-campaign.csv --profile work

# Watch logs
aws logs tail /ecs/serve-analyze-dev --follow --profile work
```

## Environment Configuration

### Development
- **Domain**: `ai-dev.goodparty.org`
- **ALB**: `ai-dev` load balancer
- **ECS Cluster**: `serve-analyze-dev`
- **Lambda Trigger**: `serve-analyze-trigger-dev`
- **S3 Bucket**: `serve-analyze-data-dev`
- **Docker Image**: `serve-analyze-dev`
- **DynamoDB Table**: `serve-message-v1-dev`

### QA
- **Domain**: `ai-qa.goodparty.org`
- **ALB**: `ai-qa` load balancer
- **ECS Cluster**: `serve-analyze-qa`
- **Lambda Trigger**: `serve-analyze-trigger-qa`
- **S3 Bucket**: `serve-analyze-data-qa`
- **Docker Image**: `serve-analyze-qa`
- **DynamoDB Table**: `serve-message-v1-qa`

### Production
- **Domain**: `ai.goodparty.org`
- **ALB**: `ai-prod` load balancer
- **ECS Cluster**: `serve-analyze-prod`
- **Lambda Trigger**: `serve-analyze-trigger-prod`
- **S3 Bucket**: `serve-analyze-data-prod`
- **Docker Image**: `serve-analyze-prod`
- **DynamoDB Table**: `serve-message-v1-prod`

## Authentication

### HTTP Trigger (via ALB)
- **Auth**: API Key in `x-api-key` header (required)
- **Validation**: ALB listener rules validate key before Lambda invocation
- **403 Response**: Invalid/missing API key returns 403 Forbidden

```bash
# HTTP POST trigger
curl -X POST https://ai-dev.goodparty.org/serve/messages/process \
  -H "Content-Type: application/json" \
  -H "x-api-key: YOUR_API_KEY_HERE" \
  -d '{
    "csvS3Path": "s3://serve-analyze-data-dev/input/campaign.csv",
    "environment": "production",
    "anonymizeKeywords": ["keyword1"]
  }'
```

### S3 Trigger (Automatic)
- **Auth**: Not required (S3 event notifications are internal)
- **Access Control**: S3 bucket is private, only Lambda can trigger
- **Upload**: Any CSV file uploaded to `input/` triggers pipeline automatically

```bash
# S3 upload trigger (no API key needed)
aws s3 cp campaign-data.csv s3://serve-analyze-data-dev/input/campaign.csv
# Pipeline starts automatically
```

## ALB Configuration

**Load Balancer Features:**
- **API Key Validation**: Enforced at ALB listener rule level before Lambda invocation
- **Target Type**: Lambda function (triggers ECS Fargate tasks)
- **SSL/TLS**: HTTPS with certificate from ACM (*.goodparty.org)
- **Path**: `/serve/messages/process` (fixed path, not wildcard)
- **Health Checks**: Not applicable (Lambda invokes ECS tasks on-demand)

**Target Group Configuration:**
```hcl
target_type = "lambda"
target_id   = Lambda ARN (serve-analyze-trigger-dev)
```

**Listener Rules:**
1. **Priority 10**: Valid requests with x-api-key → Forward to Lambda
2. **Priority 15**: Invalid requests without key → 403 Forbidden

**Why ALB + Lambda + Fargate:**
- **Unified Entry Point**: Single HTTPS endpoint for all AI services
- **Authentication at Edge**: ALB validates API keys before Lambda invocation
- **Serverless Scaling**: Fargate tasks scale to zero when idle
- **Cost Efficient**: No idle costs, pay per task execution
- **Easy Service Addition**: Add new paths without infrastructure duplication

## Deployment Frequency

### Docker Image Updates (Most Common ~5-10 minutes)
```bash
# Build new image
cd serve/v1_pipeline
docker buildx build --platform linux/arm64 -t serve-analyze-dev -f Dockerfile ../..

# Push to ECR
ECR_REPO=333022194791.dkr.ecr.us-west-2.amazonaws.com/gp-ai-projects
docker tag serve-analyze-dev:latest ${ECR_REPO}:serve-analyze-dev
aws ecr get-login-password --region us-west-2 | \
  docker login --username AWS --password-stdin ${ECR_REPO}
docker push ${ECR_REPO}:serve-analyze-dev

# ECS will use new image on next task launch
```

### Lambda Code Updates (Rare ~2 minutes)
```bash
cd infrastructure/modules/serve-analyze-fargate/lambda-trigger
npm install && npm run build
zip -r ../lambda-trigger.zip .

cd infrastructure/environments/dev/serve-analyze-fargate
AWS_PROFILE=work terraform apply  # Updates Lambda function
```

### Infrastructure Updates (Occasional ~5-10 minutes)
```bash
cd infrastructure/environments/dev/shared-infra
AWS_PROFILE=work terraform apply  # ALB/Route53/listener rules

cd infrastructure/environments/dev/serve-analyze-fargate
AWS_PROFILE=work terraform apply  # ECS cluster/S3/SNS/EventBridge
```

### When to Update Infrastructure
- **Shared ALB**: Adding new services, changing API keys, SSL certificates
- **Fargate Module**: Changing task resources (CPU/memory), environment variables, monitoring
- **Docker Image**: Code changes in pipeline (most frequent)

## Monitoring & Observability

### CloudWatch Logs
- **Lambda Trigger**: `/aws/lambda/serve-analyze-trigger-{env}`
- **ECS Tasks**: `/ecs/serve-analyze-{env}`
- **Retention**: 30 days (configurable per environment)


### CloudWatch Metrics
- **ALB**: Request count, target response time, 4xx/5xx errors
- **Lambda Trigger**: Invocations, errors, duration
- **ECS**: Running tasks, CPU/memory utilization, task failures
- **S3**: Object uploads to input/

### Failure Monitoring (Configured)
- **EventBridge Rule**: Captures ECS task failures (exit code ≠ 0)
- **SNS Topic**: `serve-analyze-pipeline-failures-{env}`
- **Slack Notifier**: Optional Lambda for Slack alerts
- **Email Alerts**: Configurable email notifications
- **CloudWatch Alarm**: High failure rate (>3 tasks in 5 minutes)

**Monitoring Features:**
- Real-time failure notifications (<60 seconds)
- Task tagging (campaign, S3 path, trigger source)
- Direct links to CloudWatch Logs in alerts
- Failure rate tracking and aggregation

For detailed monitoring setup, see:
- `infrastructure/modules/serve-analyze-fargate/FAILURE_MONITORING.md`
- `infrastructure/modules/serve-analyze-fargate/MONITORING_SUMMARY.md`
- `infrastructure/modules/serve-analyze-fargate/SLACK_SETUP.md`

## Security

### Network Security
- **HTTPS Everywhere**: SSL/TLS via ALB with ACM certificates
- **VPC Isolation**: ECS tasks run in private subnets with NAT gateway
- **Security Groups**: ECS tasks have restricted egress (HTTPS only to APIs, S3 via VPC endpoint)
- **DDoS Protection**: AWS Shield Standard via ALB
- **S3 Bucket**: Private, block public access enabled

### Access Control
- **API Key Authentication**: ALB validates x-api-key header at edge
- **IAM Roles**: Least privilege for Lambda execution and ECS task roles
- **Secrets Management**: AWS Secrets Manager for API keys (GEMINI_API_KEY, SERVE_API_KEY)
- **S3 Access**: ECS tasks only, Lambda trigger only for reads
- **ECR Access**: ECS task execution role pulls images

### Compliance
- **Data Encryption**:
  - At rest: S3 (AES-256), DynamoDB (AWS managed)
  - In transit: HTTPS everywhere, TLS 1.2+
- **Audit Logging**: CloudTrail enabled for all API calls
- **Access Logs**: ALB logs, ECS task logs, Lambda logs
- **Secrets Rotation**: Support for automatic rotation via Secrets Manager

## Troubleshooting

### Common Issues

**ALB returns 403 Forbidden:**
- Verify x-api-key header is correct
- Check ALB listener rule configuration
- Ensure API key matches value in Secrets Manager

**S3 upload doesn't trigger pipeline:**
- Verify file is uploaded to `input/` prefix with `.csv` suffix
- Check S3 bucket notification configuration
- Verify Lambda has S3 invoke permission

**ECS task fails to start:**
- Check Docker image exists in ECR
- Verify ECS task role has permissions for Secrets Manager
- Check VPC subnets and security groups
- Review Lambda trigger logs

**Pipeline execution fails:**
- Check ECS task logs: `/ecs/serve-analyze-{env}`
- Verify Secrets Manager secrets exist (GEMINI_API_KEY, SERVE_API_KEY)
- Check S3 permissions for input file access
- Verify DynamoDB table exists and is accessible

**No failure notifications:**
- Confirm SNS subscription
- Check EventBridge rule configuration
- Verify Slack webhook URL (if using Slack)

### Useful Commands

```bash
# Check ALB status
aws elbv2 describe-load-balancers --names ai-dev --profile work

# Test DNS resolution
dig ai-dev.goodparty.org

# View Lambda trigger logs
aws logs tail /aws/lambda/serve-analyze-trigger-dev --follow --profile work

# View ECS task logs
aws logs tail /ecs/serve-analyze-dev --follow --profile work

# List running ECS tasks
aws ecs list-tasks --cluster serve-analyze-dev --profile work

# Describe specific task
aws ecs describe-tasks --cluster serve-analyze-dev --tasks TASK_ARN --profile work

# Check S3 bucket notifications
aws s3api get-bucket-notification-configuration \
  --bucket serve-analyze-data-dev --profile work

# Test HTTP trigger
curl -X POST https://ai-dev.goodparty.org/serve/messages/process \
  -H "Content-Type: application/json" \
  -H "x-api-key: YOUR_KEY" \
  -d '{"csvS3Path":"s3://serve-analyze-data-dev/input/test.csv"}'

# Test S3 trigger
aws s3 cp test.csv s3://serve-analyze-data-dev/input/test-$(date +%s).csv --profile work
```

## Future Enhancements

### Planned Additions
1. **ML Service**: `/ml/*` path for machine learning inference APIs
2. **Analytics Service**: `/analytics/*` path for data insights and reporting
3. **Step Functions**: Orchestrate complex multi-stage pipelines
4. **Multi-Region**: Global deployment for better performance and resilience
5. **WAF Integration**: Web Application Firewall for enhanced security

### Infrastructure Improvements
1. **ECS Auto Scaling**: Scale Fargate tasks based on queue depth
2. **S3 Batch Operations**: Process large batches of files efficiently
3. **Cost Optimization**: Fargate Spot for non-critical workloads
4. **Monitoring Dashboard**: CloudWatch dashboard for operational metrics
5. **CI/CD Pipeline**: Automated Docker builds and deployments via GitHub Actions
6. **Automated Retry**: Lambda-based retry logic for failed tasks
7. **Data Archival**: Lifecycle policies for S3 data retention

### Documentation
- ✅ **FAILURE_MONITORING.md**: Complete failure detection and recovery guide
- ✅ **MONITORING_SUMMARY.md**: Quick reference for monitoring
- ✅ **SLACK_SETUP.md**: Slack integration for failure alerts
- ✅ **S3_TRIGGER_DEPLOYMENT.md**: S3 event trigger deployment guide
- ✅ **USAGE.md**: End-to-end usage examples
- ✅ **ENVIRONMENTS.md**: Environment comparison and deployment flow

---

**🚀 Production-Ready Infrastructure!**

This infrastructure implements AWS best practices for serverless AI workloads:
- **Cost Efficient**: Pay only for task execution time (scale to zero)
- **Highly Available**: Multi-AZ deployment with automatic failover
- **Scalable**: Fargate scales horizontally, handles concurrent pipelines
- **Observable**: Complete monitoring with real-time failure alerts
- **Secure**: Network isolation, encryption at rest/transit, IAM least privilege
- **Maintainable**: Infrastructure as code, modular Terraform design