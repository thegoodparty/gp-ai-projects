# Environment Management Guide

## Overview

The Serve Messages API supports multiple environments with isolated infrastructure and configurations. This guide covers managing development, staging, and production environments.

## Environment Structure

### Supported Environments
- **dev**: Development environment for testing and feature development
- **staging**: Pre-production environment for integration testing (future)
- **prod**: Production environment for live traffic (future)

### Environment Isolation

Each environment has:
- Separate DynamoDB tables (`serve-messages-{env}`)
- Separate Lambda functions (`serve-message-{env}`)
- Separate Application Load Balancers (`serve-messages-{env}`)
- Separate DNS subdomains
- Isolated IAM roles and policies

## Current Environment Configuration

### Development Environment (`dev`)

**Infrastructure:**
- **Region**: us-west-2
- **DynamoDB Table**: `serve-messages-dev`
- **Lambda Function**: `serve-message-dev`
- **ALB Name**: `serve-messages-dev`
- **Custom Domain**: `ai-dev.goodparty.org`
- **Lambda ARN**: `arn:aws:lambda:us-west-2:333022194791:function:serve-message-dev`

**Configuration Files:**
```
infrastructure/serve-message-api/deploy/terraform/environments/dev.tfvars
infrastructure/shared/environments/dev.tfvars
```

**API Access:**
- **Base URL**: `https://ai-dev.goodparty.org/serve/messages/{campaign_id}`
- **API Key**: `YOUR_API_KEY_HERE`
- **Authentication**: API key in `x-api-key` header

## Environment Management Tasks

### 1. Creating a New Environment

To create a staging environment:

#### Step 1: Create Terraform Variable Files

```bash
# Create Lambda infrastructure variables
cp infrastructure/serve-message-api/deploy/terraform/environments/dev.tfvars \
   infrastructure/serve-message-api/deploy/terraform/environments/staging.tfvars

# Create shared infrastructure variables
cp infrastructure/shared/environments/dev.tfvars \
   infrastructure/shared/environments/staging.tfvars
```

#### Step 2: Update Configuration

Edit `staging.tfvars` files:

**Lambda Infrastructure (`serve-message-api/environments/staging.tfvars`):**
```hcl
environment = "staging"
```

**Shared Infrastructure (`shared/environments/staging.tfvars`):**
```hcl
environment = "staging"
custom_domain_name = "ai-staging.goodparty.org"
vpc_id = "vpc-0763fa52c32ebcf6a"  # Same VPC or separate for isolation
public_subnet_ids = ["subnet-...", "subnet-..."]
serve_message_lambda_arn = "arn:aws:lambda:us-west-2:333022194791:function:serve-message-staging"
serve_message_lambda_function_name = "serve-message-staging"
api_key = "STAGING_API_KEY_HERE"  # Different key for staging
certificate_arn = "arn:aws:acm:..."  # Certificate covering staging domain
route53_zone_id = "Z10392302OXMPNQLPO07K"
```

#### Step 3: Deploy Infrastructure

```bash
# Deploy Lambda infrastructure
cd infrastructure/serve-message-api/deploy/terraform
AWS_PROFILE=work terraform workspace new staging  # Optional: use workspaces
AWS_PROFILE=work terraform apply -var-file=environments/staging.tfvars

# Note the Lambda ARN from outputs, update shared/environments/staging.tfvars

# Deploy shared infrastructure (ALB + Route53)
cd ../../../../infrastructure/shared
AWS_PROFILE=work terraform apply -var-file=environments/staging.tfvars

# Deploy Lambda code
cd ../../serve/messages
./deploy-lambdas.sh -e staging -f serve-message
```

### 2. Switching Between Environments

#### Deploy to Specific Environment

```bash
# Development
./deploy-lambdas.sh -e dev -f serve-message

# Staging (when created)
./deploy-lambdas.sh -e staging -f serve-message

# Production (when created)
./deploy-lambdas.sh -e prod -f serve-message
```

#### Check Current Environment Status

```bash
# List all Lambda functions
AWS_PROFILE=work aws lambda list-functions \
    --query 'Functions[?starts_with(FunctionName, `serve-message`)].{Name:FunctionName,Runtime:Runtime,LastModified:LastModified}' \
    --output table

# Check DynamoDB tables
AWS_PROFILE=work aws dynamodb list-tables \
    --query 'TableNames[?starts_with(@, `serve-messages`)]' \
    --output table
```

### 3. Environment-Specific Testing

#### Development Environment Testing

```bash
# Test GET
curl -X GET "https://ai-dev.goodparty.org/serve/messages/test-campaign" \
    -H "x-api-key: YOUR_API_KEY_HERE"

# Test POST
curl -X POST "https://ai-dev.goodparty.org/serve/messages/test-campaign" \
    -H "x-api-key: YOUR_API_KEY_HERE" \
    -H "Content-Type: application/json" \
    -d '{"campaign_id":"test-campaign","voter_name":"Dev Test","response":"Development test message"}'
```

#### Environment Health Check Script

```bash
#!/bin/bash
# health-check.sh

ENVIRONMENT=${1:-dev}
API_KEY="YOUR_API_KEY_HERE"

case $ENVIRONMENT in
    dev)
        BASE_URL="https://ai-dev.goodparty.org"
        ;;
    staging)
        BASE_URL="https://ai-staging.goodparty.org"
        ;;
    prod)
        BASE_URL="https://ai.goodparty.org"
        ;;
    *)
        echo "Unknown environment: $ENVIRONMENT"
        exit 1
        ;;
esac

echo "Testing $ENVIRONMENT environment..."

# Test GET endpoint
echo "Testing GET..."
RESPONSE=$(curl -s -X GET "$BASE_URL/serve/messages/health-check" \
    -H "x-api-key: $API_KEY")

if [[ $? -eq 0 ]]; then
    echo "✅ GET request successful"
else
    echo "❌ GET request failed"
fi

# Test POST endpoint
echo "Testing POST..."
RESPONSE=$(curl -s -X POST "$BASE_URL/serve/messages/health-check" \
    -H "x-api-key: $API_KEY" \
    -H "Content-Type: application/json" \
    -d '{"campaign_id":"health-check","test":"true"}')

if [[ $? -eq 0 ]]; then
    echo "✅ POST request successful"
else
    echo "❌ POST request failed"
fi

echo "Health check complete for $ENVIRONMENT"
```

### 4. Data Management Between Environments

#### Copying Data from Dev to Staging

```bash
# Export data from dev
AWS_PROFILE=work aws dynamodb scan \
    --table-name serve-messages-dev \
    --output json > dev-data-backup.json

# Import data to staging (requires staging environment to exist)
# Note: This is a simplified example - production use should handle pagination
AWS_PROFILE=work aws dynamodb batch-write-item \
    --request-items file://staging-import.json
```

#### Environment Data Isolation

**Important**: Each environment maintains separate data:
- Development data should be test/synthetic data only
- Production data must never be copied to development
- Use separate AWS accounts for maximum isolation in production

### 5. Environment Monitoring

#### CloudWatch Dashboards

Create environment-specific dashboards:

```bash
# Lambda metrics
AWS_PROFILE=work aws cloudwatch put-dashboard \
    --dashboard-name "serve-message-dev" \
    --dashboard-body file://cloudwatch-dashboard-dev.json
```

#### Log Monitoring

```bash
# Monitor logs by environment
AWS_PROFILE=work aws logs tail /aws/lambda/serve-message-dev --follow
AWS_PROFILE=work aws logs tail /aws/lambda/serve-message-staging --follow
AWS_PROFILE=work aws logs tail /aws/lambda/serve-message-prod --follow
```


## Environment Configuration Reference

### Required Environment Variables

Each environment needs these configured in Terraform:

```hcl
# Lambda infrastructure environments/{env}.tfvars
environment = "{env}"  # dev, staging, prod

# Shared infrastructure environments/{env}.tfvars
environment = "{env}"
custom_domain_name = "ai-{env}.goodparty.org"  # Environment-specific domain
vpc_id = "vpc-..."  # VPC for ALB
public_subnet_ids = ["subnet-...", "subnet-..."]  # Public subnets for ALB
serve_message_lambda_arn = "arn:aws:lambda:..."  # From Lambda deployment
serve_message_lambda_function_name = "serve-message-{env}"
api_key = "..."  # Environment-specific API key
certificate_arn = "arn:aws:acm:..."  # SSL certificate for domain
route53_zone_id = "Z..."  # Route53 hosted zone ID
```

### Environment-Specific Resources

**Naming Convention**: All resources include environment suffix
- DynamoDB: `serve-messages-{env}`
- Lambda: `serve-message-{env}`
- ALB: `serve-messages-{env}`
- Target Group: `serve-message-{env}`
- CloudWatch Logs: `/aws/lambda/serve-message-{env}`
- IAM Roles: `campaign-data-*-lambda-role-{env}`

## Security Considerations

### Environment Separation
- Different API keys per environment (recommended for prod)
- Separate IAM roles with environment-specific permissions
- Network isolation (VPC) for production environment

### Access Control
- Development: Broader access for testing
- Staging: Production-like restrictions
- Production: Minimal access, audit logging

### API Key Management

**Current Approach** (Development):
- Single API key configured in ALB listener rules
- Shared across all environments

**Recommended Approach** (Production):
- Environment-specific API keys in ALB listener rules
- Stored in AWS Secrets Manager
- Rotated regularly by updating listener rule conditions
- Usage tracking per environment via ALB access logs

## Troubleshooting Environment Issues

### Common Environment Problems

#### 1. Wrong Environment Deployment
```bash
# Check which environment you're in
aws sts get-caller-identity
terraform workspace show  # If using workspaces

# Verify function exists in target environment
AWS_PROFILE=work aws lambda get-function --function-name serve-message-{env}
```

#### 2. Cross-Environment Data Access
```bash
# Verify table names
AWS_PROFILE=work aws dynamodb describe-table --table-name serve-messages-{env}

# Check IAM permissions
AWS_PROFILE=work aws iam get-role-policy --role-name campaign-data-set-lambda-role-{env} --policy-name policy-name
```

#### 3. DNS/Domain Issues
```bash
# Check ALB configuration
AWS_PROFILE=work aws elbv2 describe-load-balancers \
    --names serve-messages-{env} \
    --region us-west-2

# Check Route53 record
AWS_PROFILE=work aws route53 list-resource-record-sets \
    --hosted-zone-id Z10392302OXMPNQLPO07K \
    --query "ResourceRecordSets[?Name=='ai-{env}.goodparty.org.']"

# Test DNS resolution
nslookup ai-{env}.goodparty.org
dig ai-{env}.goodparty.org
```

### Environment Recovery Procedures

#### 1. Rebuild Environment from Scratch
```bash
# Destroy infrastructure
cd infrastructure/serve-message-api/deploy/terraform
AWS_PROFILE=work terraform destroy -var-file=environments/{env}.tfvars

# Redeploy infrastructure
AWS_PROFILE=work terraform apply -var-file=environments/{env}.tfvars -auto-approve

# Redeploy Lambda code
cd ../../../serve/messages
./deploy-lambdas.sh -e {env} -f serve-message
```

#### 2. Rollback to Previous Version
```bash
# Find previous deployment
git log --oneline

# Checkout previous version
git checkout {previous-commit}

# Redeploy
./deploy-lambdas.sh -e {env} -f serve-message
```

## Future Environment Planning

### Production Environment Recommendations

When creating production environment:

1. **Separate AWS Account**: Use dedicated AWS account for production
2. **Enhanced Monitoring**: CloudWatch alarms, SNS notifications
3. **Backup Strategy**: DynamoDB point-in-time recovery
4. **Security**: WAF, enhanced API key management
5. **Compliance**: Enable CloudTrail, Config, GuardDuty

### Staging Environment Benefits

1. **Integration Testing**: Test with production-like configuration
2. **Performance Testing**: Load testing without affecting production
3. **Deployment Validation**: Test infrastructure changes safely

---

**Last Updated**: September 26, 2025
**Version**: Unified Lambda Architecture
**Supported Environments**: dev (active), staging (planned), prod (planned)