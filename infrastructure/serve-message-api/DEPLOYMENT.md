# Serve Messages API - Deployment Guide

## Overview

This guide covers the complete deployment process for the Serve Messages API unified architecture. The system consists of two main infrastructure components that must be deployed in order.

## Architecture Components

### 1. Lambda Infrastructure (`infrastructure/serve-message-api/`)
- DynamoDB table (`serve-messages-{env}`)
- IAM roles and policies
- Unified Lambda function (`serve-message-{env}`)

### 2. Shared Infrastructure (`infrastructure/shared/`)
- Application Load Balancer (ALB) with HTTPS listener
- Lambda target groups with health checks
- ALB listener rules for API key validation
- Route53 DNS configuration

## Prerequisites

### Required Tools
- **AWS CLI** configured with appropriate credentials
- **Terraform** v1.0+ installed
- **Node.js** 22+ with npm (for TypeScript compilation)
- **AWS Profile**: `work` configured with proper permissions

### Required AWS Permissions
- DynamoDB table creation/management
- Lambda function deployment and management
- Application Load Balancer management
- Route53 hosted zone management
- IAM role/policy creation
- CloudWatch logs access

## Deployment Process

### Step 1: Deploy Lambda Infrastructure

```bash
# Navigate to Lambda infrastructure
cd infrastructure/serve-message-api/deploy/terraform

# Initialize Terraform (first time only)
terraform init

# Plan deployment (optional - review changes)
AWS_PROFILE=work terraform plan -var-file=environments/dev.tfvars

# Deploy infrastructure
AWS_PROFILE=work terraform apply -var-file=environments/dev.tfvars -auto-approve
```

**What this creates:**
- DynamoDB table: `serve-messages-dev`
- Lambda function: `serve-message-dev`
- Lambda ARN (needed for next step)
- IAM roles with least-privilege permissions
- CloudWatch log group

### Step 2: Update ALB Configuration

```bash
# Navigate to shared infrastructure
cd ../../../../infrastructure/shared

# Verify dev.tfvars has correct Lambda ARN from Step 1
# serve_message_lambda_arn = "arn:aws:lambda:us-west-2:333022194791:function:serve-message-dev"
# serve_message_lambda_function_name = "serve-message-dev"

# Deploy ALB infrastructure
AWS_PROFILE=work terraform apply -var-file=environments/dev.tfvars -auto-approve
```

**What this creates:**
- Application Load Balancer with HTTPS listener
- Lambda target group with health checks
- ALB listener rules for API key validation
- Route53 A record pointing to ALB

### Step 3: Deploy Lambda Code

```bash
# Navigate to Lambda code directory
cd ../../serve/messages

# Deploy the unified Lambda function
./deploy-lambdas.sh -e dev -f serve-message
```

**What this does:**
- Installs npm dependencies in `lambdas/serve-message/`
- Compiles TypeScript to JavaScript
- Creates deployment zip package
- Updates Lambda function code
- Verifies deployment

## Environment Configuration

### Development Environment (`dev`)

Current configuration:

```bash
# DynamoDB
TABLE_NAME=serve-messages-dev

# Lambda Function
FUNCTION_NAME=serve-message-dev
LAMBDA_ARN=arn:aws:lambda:us-west-2:333022194791:function:serve-message-dev

# ALB Configuration
ALB_NAME=serve-messages-dev
DOMAIN=ai-dev.goodparty.org
API_KEY=YOUR_API_KEY_HERE

# Region
AWS_REGION=us-west-2
```

### Production Environment (`prod`)

To deploy to production:

```bash
# Deploy Lambda infrastructure
cd infrastructure/serve-message-api/deploy/terraform
AWS_PROFILE=work terraform apply -var-file=environments/prod.tfvars -auto-approve

# Deploy ALB and Route53 infrastructure
cd ../../../../infrastructure/shared
AWS_PROFILE=work terraform apply -var-file=environments/prod.tfvars -auto-approve

# Deploy Lambda code
cd ../../serve/messages
./deploy-lambdas.sh -e prod -f serve-message
```

## Verification Steps

### 1. Verify ALB Health Check

```bash
# Test health endpoint (no API key required)
curl -X GET "https://ai-dev.goodparty.org/serve/messages/health"
```

Expected response:
```json
{
  "status": "healthy",
  "timestamp": "2025-09-29T...",
  "service": "serve-message",
  "environment": "dev"
}
```

### 2. Verify GET Functionality

```bash
# Test GET via ALB
curl -X GET "https://ai-dev.goodparty.org/serve/messages/test-campaign" \
    -H "x-api-key: YOUR_API_KEY_HERE"
```

### 3. Verify POST Functionality

```bash
# Test POST via ALB
curl -X POST "https://ai-dev.goodparty.org/serve/messages/test-campaign" \
    -H "x-api-key: YOUR_API_KEY_HERE" \
    -H "Content-Type: application/json" \
    -d '{"campaign_id":"test-campaign","voter_name":"Test User","response":"Test message"}'
```

### 4. Verify API Key Validation

```bash
# Test without API key (should return 403)
curl -X GET "https://ai-dev.goodparty.org/serve/messages/test-campaign"
```

Expected: 403 Forbidden

## Common Deployment Issues

### Issue 1: ALB Returns 502/503 Errors
**Symptoms**: ALB returns 502 Bad Gateway or 503 Service Unavailable
**Solution**: Check Lambda target group health and permissions

```bash
# Check target group health
AWS_PROFILE=work aws elbv2 describe-target-health \
    --target-group-arn <target-group-arn> \
    --region us-west-2

# Verify Lambda permission for ALB
AWS_PROFILE=work aws lambda get-policy \
    --function-name serve-message-dev \
    --region us-west-2
```

### Issue 2: TypeScript Compilation Errors
**Symptoms**: Deploy script fails during build phase
**Solution**: Check and fix TypeScript errors

```bash
# Manual build to see errors
cd serve/messages/lambdas/serve-message
npm run build

# Fix any TypeScript errors in src/ files
```

### Issue 3: DynamoDB Permissions
**Symptoms**: Lambda returns 500 errors with DynamoDB access denied
**Solution**: Verify IAM role has correct permissions

```bash
# Check CloudWatch logs
AWS_PROFILE=work aws logs tail /aws/lambda/serve-message-dev --follow --region us-west-2
```

### Issue 4: API Key Authentication Fails
**Symptoms**: ALB returns 403 Forbidden
**Solution**: Verify API key in request headers and ALB listener rules

```bash
# Ensure x-api-key header is included
curl -H "x-api-key: YOUR_API_KEY_HERE" \
    "https://ai-dev.goodparty.org/serve/messages/test-campaign"

# Check ALB listener rules
AWS_PROFILE=work aws elbv2 describe-rules \
    --listener-arn <listener-arn> \
    --region us-west-2
```

## Rollback Procedures

### Rolling Back Lambda Code

```bash
# Deploy previous version from git
cd serve/messages
git checkout <previous-commit>
./deploy-lambdas.sh -e dev -f serve-message
```

### Rolling Back Infrastructure

```bash
# Revert Terraform changes
cd infrastructure/serve-message-api/deploy/terraform
git checkout <previous-commit>
AWS_PROFILE=work terraform apply -var-file=environments/dev.tfvars -auto-approve
```

## Maintenance Tasks

### Updating Lambda Code Only

```bash
cd serve/messages
./deploy-lambdas.sh -e dev -f serve-message
```

### Updating Infrastructure Only

```bash
cd infrastructure/serve-message-api/deploy/terraform
AWS_PROFILE=work terraform plan -var-file=environments/dev.tfvars
AWS_PROFILE=work terraform apply -var-file=environments/dev.tfvars
```

### Viewing Logs

```bash
# Real-time log monitoring
AWS_PROFILE=work aws logs tail /aws/lambda/serve-message-dev --follow --region us-west-2

# Search logs for errors
AWS_PROFILE=work aws logs filter-log-events \
    --log-group-name /aws/lambda/serve-message-dev \
    --filter-pattern "ERROR" \
    --region us-west-2
```

## Environment Variables Reference

### Terraform Variables (`environments/dev.tfvars`)

**Lambda Infrastructure** (`infrastructure/serve-message-api/deploy/terraform/environments/dev.tfvars`):
```hcl
environment = "dev"
```

**Shared Infrastructure** (`infrastructure/shared/environments/dev.tfvars`):
```hcl
environment = "dev"
custom_domain_name = "ai-dev.goodparty.org"
vpc_id = "vpc-0763fa52c32ebcf6a"
public_subnet_ids = ["subnet-07984b965dabfdedc", "subnet-01c540e6428cdd8db"]
serve_message_lambda_arn = "arn:aws:lambda:us-west-2:333022194791:function:serve-message-dev"
serve_message_lambda_function_name = "serve-message-dev"
api_key = "YOUR_API_KEY_HERE"
certificate_arn = "arn:aws:acm:us-west-2:333022194791:certificate/877b533e-4a54-4a63-8ed4-55818f0d8d34"
route53_zone_id = "Z10392302OXMPNQLPO07K"
```

### Lambda Environment Variables

These are automatically set by Terraform:

```bash
TABLE_NAME=serve-messages-dev
ENVIRONMENT=dev
```

## Security Considerations

### API Key Management
- API key is configured in ALB listener rules
- For production, consider using AWS Secrets Manager
- Rotate API keys periodically by updating listener rules

### IAM Permissions
- Lambda uses least-privilege IAM policies
- DynamoDB access limited to specific table
- CloudWatch logs access for debugging
- ALB has permission to invoke Lambda function

### Network Security
- HTTPS only with SSL/TLS termination at ALB
- API key validation at ALB listener level
- Lambda in VPC for additional security (optional)
- Security groups control ALB access


---

**Last Updated**: September 26, 2025
**Version**: Unified Lambda Architecture
**Region**: us-west-2