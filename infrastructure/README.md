# Infrastructure

AWS infrastructure for the GoodParty.org AI platform using a **multi-service Application Load Balancer architecture** with path-based routing.

## Architecture Philosophy

This infrastructure is designed to support **multiple microservices** under a single ALB:
- One ALB routes to multiple Lambda/ECS services via path patterns
- Services are added by creating new target groups and listener rules
- Shared authentication (API key) across all services
- Efficient: Single ALB serves unlimited services

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
├── shared/                          # Deploy ONCE per environment
│   ├── main.tf                     # ALB + Route53 modules
│   ├── variables.tf                # Shared infrastructure variables
│   ├── outputs.tf                  # ALB/Route53 outputs
│   ├── deploy.sh                   # Shared infrastructure deployment script
│   ├── environments/
│   │   ├── dev.tfvars              # ai-dev.goodparty.org configuration
│   │   └── prod.tfvars             # ai.goodparty.org configuration
│   ├── alb/                        # Application Load Balancer module
│   │   ├── main.tf
│   │   ├── variables.tf
│   │   └── outputs.tf
│   ├── route53/                    # Route53 DNS module
│   │   ├── main.tf
│   │   ├── variables.tf
│   │   └── outputs.tf
│   └── ecr/                        # Shared ECR repository
│       ├── main.tf                 # ECR repository + lifecycle policy
│       └── README.md               # ECR documentation
│
├── serve-message-api/              # Deploy FREQUENTLY
│   ├── lambdas/                    # TypeScript Lambda functions
│   │   ├── set_campaign_data/      # SET operations (write data)
│   │   └── retrieve_campaign_data/ # RETRIEVE operations (read/filter)
│   └── deploy/
│       ├── terraform/              # Service-specific infrastructure
│       │   ├── main.tf             # DynamoDB + Lambda + API Gateway
│       │   ├── modules/            # Service modules
│       │   └── environments/       # Service environment configs
│       └── scripts/
│           └── deploy.sh           # Service deployment script
│
└── README.md                       # This file
```

## Architecture Overview

### Multi-Service ALB Architecture

```
                      Internet
                         ↓
                   Route53 DNS
                         ↓
            Application Load Balancer
                         ↓
        ┌────────────────┼────────────────┐
        │                │                │
    /serve/messages/*  /ml/*      /analytics/*
        ↓                ↓                ↓
   serve-message    ml-inference    analytics
      Lambda          Lambda          Lambda
        ↓                ↓                ↓
    DynamoDB         Model S3       Analytics DB
```

**Current Services:**
- ✅ `/serve/messages/*` - Campaign message API (production)
- 🔜 `/ml/*` - Machine learning inference (planned)
- 🔜 `/analytics/*` - Data analytics API (planned)

### Two-Tier Deployment Model

```
Internet → Route53 → ALB → Lambda Target Groups → Backends
           ↑        ↑      ↑
         Shared   Shared  Service (per-service)
```

### Shared Infrastructure (Deploy Once)
- **Application Load Balancer (ALB)**: Single HTTPS load balancer serving all services
- **Route53**: DNS records pointing to ALB
- **Listener Rules**: Path-based routing with API key validation

### Service Infrastructure (Deploy Per Service)
- **Lambda Functions**: Service-specific Lambda functions
- **Target Groups**: Each service has its own Lambda target group
- **Databases**: Service-specific data stores (DynamoDB, RDS, etc.)
- **IAM**: Service-specific roles and policies

## Deployment Workflow

### Prerequisites
- AWS CLI configured with `work` profile
- Terraform 1.0+ installed
- Node.js 22+ for Lambda builds
- Access to `goodparty.org` hosted zone and `*.goodparty.org` certificate

### Step 1: Deploy Service Infrastructure
```bash
cd infrastructure/serve-message-api/deploy/terraform
AWS_PROFILE=work terraform apply -var-file=environments/dev.tfvars -auto-approve
```

**What this creates:**
```
DynamoDB Table: serve-messages-dev
Lambda Function: serve-message-dev
Lambda ARN: arn:aws:lambda:us-west-2:333022194791:function:serve-message-dev
```

### Step 2: Update Shared Configuration
Edit `infrastructure/shared/environments/dev.tfvars`:
```hcl
# Update Lambda ARN from Step 1
serve_message_lambda_arn = "arn:aws:lambda:us-west-2:333022194791:function:serve-message-dev"
serve_message_lambda_function_name = "serve-message-dev"
```

### Step 3: Deploy Shared Infrastructure (ALB)
```bash
cd infrastructure/shared
AWS_PROFILE=work terraform apply -var-file=environments/dev.tfvars -auto-approve
```

**Output Example:**
```
ALB DNS: serve-messages-dev-123456789.us-west-2.elb.amazonaws.com
Custom Domain: https://ai-dev.goodparty.org
```

### Step 4: Verify Endpoints
```bash
# Test via custom domain (ALB)
GET  https://ai-dev.goodparty.org/serve/messages/{campaign_id}
POST https://ai-dev.goodparty.org/serve/messages/{campaign_id}

# Both require x-api-key header
curl -H "x-api-key: YOUR_API_KEY_HERE" \
    "https://ai-dev.goodparty.org/serve/messages/test-campaign"
```

## Environment Configuration

### Development
- **Domain**: `ai-dev.goodparty.org`
- **ALB**: `serve-messages-dev` load balancer
- **Lambda**: `serve-message-dev` function
- **DynamoDB**: `serve-messages-dev` table

### Production
- **Domain**: `ai.goodparty.org`
- **ALB**: `serve-messages-prod` load balancer
- **Lambda**: `serve-message-prod` function
- **DynamoDB**: `serve-messages-prod` table

## Authentication

### All Operations (GET and POST)
- **Auth**: API Key in `x-api-key` header
- **Validation**: ALB listener rules check for valid API key
- **Rate Limits**: ALB native rate limiting

```bash
# GET requests
curl -H "x-api-key: YOUR_API_KEY_HERE" \
  "https://ai-dev.goodparty.org/serve/messages/campaign-123?age_min=25"

# POST requests
curl -X POST -H "x-api-key: YOUR_API_KEY_HERE" \
  -H "Content-Type: application/json" \
  -d '{"campaign_id":"campaign-123","voter_name":"Alice"}' \
  "https://ai-dev.goodparty.org/serve/messages/campaign-123"
```

## ALB Configuration

**Load Balancer Features:**
- **API Key Validation**: Enforced at ALB listener rule level
- **Health Checks**: Disabled for Lambda targets (on-demand invocation)
- **SSL/TLS**: HTTPS with certificate from ACM
- **Target Groups**: Direct Lambda invocation

**Lambda Response Headers:**
```javascript
'Cache-Control': 'no-cache, no-store, must-revalidate',
'Pragma': 'no-cache',
'Expires': '0'
```

**Why Multi-Service ALB Architecture:**
- **Resource Efficient**: Single ALB serves unlimited services
- **Scalable**: Easy to add new services without infrastructure duplication
- **Unified Authentication**: Shared API key validation across all services
- **Direct Integration**: Lambda target groups for serverless execution
- **Immediate Deployments**: No CDN propagation delays
- **Path-Based Routing**: `/serve/messages/*`, `/ml/*`, `/analytics/*`, etc.

## Deployment Frequency

### Daily Development (Fast ~1 minute)
```bash
cd serve/messages
./deploy-lambdas.sh -e dev -f serve-message  # Lambda code updates only
```

### Monthly/Quarterly (Moderate ~5 minutes)
```bash
cd infrastructure/shared
AWS_PROFILE=work terraform apply -var-file=environments/dev.tfvars  # ALB/Route53 updates
```

### When to Update Shared Infrastructure
- Adding new Lambda target groups
- Modifying ALB listener rules
- Changing API key
- SSL certificate updates
- Route53 DNS changes

## Health Check Architecture

### Current Configuration

Health checks are **disabled** for Lambda target groups. This is the recommended approach for serverless functions because:

- Lambda functions are invoked on-demand by ALB
- AWS manages Lambda infrastructure health automatically
- Failed invocations are handled at request time with appropriate error responses
- Eliminates unnecessary periodic health check invocations

**Current Setup:**
```
ALB Target Group (Lambda) → health_check.enabled = false
```

### Alternative: Enable Health Checks (Not Recommended for Lambda)

If you want to add health checks for monitoring purposes, you have two options:

#### Option 1: ALB-Level Health Endpoint (Recommended)

Create a dedicated health check endpoint that's independent of any service:

```
ALB Health Check: GET /health (no API key required)
├── Simple health Lambda returning 200 OK
└── Used for ALB-level monitoring

Service-Specific Target Groups:
├── /serve/messages/* → serve-message Lambda
│   └── Target Group Health: /serve/messages/health
├── /ml/* → ml-inference Lambda/ECS
│   └── Target Group Health: /ml/health
├── /analytics/* → analytics Lambda/ECS
│   └── Target Group Health: /analytics/health
└── Future services...
```

**Implementation Plan:**
1. Create lightweight health-check Lambda that returns `{"status": "healthy", "timestamp": "..."}`
2. Add ALB listener rule at priority 1: `/health` → health-check target group (no API key)
3. Each service keeps its own health endpoint for target group monitoring
4. ALB overall health becomes independent of any single service

**Benefits:**
- Cleaner separation: ALB health vs service health
- Easy to add new services without touching ALB health config
- Standard microservices pattern
- Route53 health checks can use `/health` for failover

#### Option 2: Dynamic Target Group Configuration

Refactor the ALB module to accept multiple target configurations:

```hcl
# infrastructure/shared/alb/variables.tf
variable "service_targets" {
  type = list(object({
    name          = string
    path_pattern  = string
    health_path   = string
    lambda_arn    = string
    lambda_name   = string
  }))
}

# Example configuration
service_targets = [
  {
    name          = "serve-message"
    path_pattern  = "/serve/messages/*"
    health_path   = "/serve/messages/health"
    lambda_arn    = "arn:aws:lambda:..."
    lambda_name   = "serve-message-dev"
  },
  {
    name          = "ml-inference"
    path_pattern  = "/ml/*"
    health_path   = "/ml/health"
    lambda_arn    = "arn:aws:lambda:..."
    lambda_name   = "ml-inference-dev"
  }
]
```

**Implementation Plan:**
1. Refactor `infrastructure/shared/alb/main.tf` to use `for_each` for target groups
2. Remove hardcoded `serve_message_lambda_arn` variable
3. Each service defines its own health check path
4. Dynamically create listener rules based on service list

**Benefits:**
- More flexible and scalable
- Each service is self-contained
- Easy to add/remove services

**Trade-offs:**
- More complex Terraform code
- Requires refactoring existing setup

### Recommendation for Multi-Service Architecture

If you add EC2/ECS targets that require health checks, implement **Option 1** for ALB-level monitoring. Lambda targets can keep health checks disabled.

## Multi-Service Architecture Pattern

### How It Works

The ALB uses **path-based routing** with **priority-based listener rules**:

```hcl
Priority 10: /serve/messages/health    → serve-message (no API key)
Priority 20: /serve/messages/* + key   → serve-message Lambda
Priority 30: /serve/messages/* no key  → 403 Forbidden

Priority 40: /ml/* + key               → ml-inference Lambda  (future)
Priority 50: /ml/* no key              → 403 Forbidden        (future)

Default: /*                            → 404 Not Found
```

Each service gets:
1. **Target Group**: Lambda/ECS target group with health checks
2. **Valid Request Rule**: Path pattern + API key → forward to service
3. **Invalid Request Rule**: Path pattern without key → 403 Forbidden

### Benefits of Single ALB

- **Single Entry Point**: One domain (`ai-dev.goodparty.org`) for all AI services
- **Shared Security**: API key validation enforced at ALB level for all services
- **Resource Optimization**: Single ALB serves multiple services vs dedicated ALB per service
- **Simplified DNS**: One Route53 record for all services
- **Unified Monitoring**: Centralized ALB access logs and metrics

## Adding New Services

To add a new service (e.g., ML API):

### If Using Option 1 (Current + Health Endpoint)

1. **Create service Lambda**:
   ```bash
   mkdir infrastructure/ml-api
   # Implement Lambda with /ml/health endpoint
   ```

2. **Update ALB module** (add new target group):
   ```hcl
   # infrastructure/shared/alb/main.tf
   resource "aws_lb_target_group" "ml_inference" {
     name        = "ml-inference-${var.environment}"
     target_type = "lambda"

     health_check {
       enabled   = true
       path      = "/ml/health"
       matcher   = "200"
       interval  = 30
     }
   }

   resource "aws_lb_listener_rule" "ml_inference" {
     listener_arn = aws_lb_listener.https.arn
     priority     = 40

     action {
       type             = "forward"
       target_group_arn = aws_lb_target_group.ml_inference.arn
     }

     condition {
       path_pattern {
         values = ["/ml/*"]
       }
     }

     condition {
       http_header {
         http_header_name = "x-api-key"
         values          = [var.api_key]
       }
     }
   }
   ```

3. **Deploy in order**:
   ```bash
   # 1. Deploy service Lambda
   cd infrastructure/ml-api
   AWS_PROFILE=work terraform apply

   # 2. Update ALB with new target group
   cd infrastructure/shared
   AWS_PROFILE=work terraform apply -var-file=environments/dev.tfvars
   ```

### If Using Option 2 (Dynamic Configuration)

1. **Create service Lambda with health endpoint**
2. **Add to `service_targets` list** in `environments/dev.tfvars`
3. **Run terraform apply** - target group created automatically


## Monitoring & Observability

### CloudWatch Logs
- **Lambda functions**: `/aws/lambda/serve-message-*`
- **API Gateway**: Access logs enabled
- **Retention**: 7 days

### CloudWatch Metrics
- **Lambda**: Duration, errors, invocations
- **API Gateway**: Request count, latency, 4xx/5xx errors
- **DynamoDB**: Read/write capacity, throttles

### Alarms (Future)
- Lambda error rate > 5%
- API Gateway latency > 2 seconds
- DynamoDB throttling events

## Security

### Network Security
- **HTTPS Everywhere**: SSL/TLS end-to-end
- **CORS**: Configured for web applications
- **DDoS Protection**: AWS Shield Standard via ALB

### Access Control
- **IAM Roles**: Least privilege for Lambda functions
- **API Keys**: Rate limiting and usage quotas
- **Secrets**: No hardcoded credentials
- **VPC**: Lambda functions in default VPC (can be moved to private)

### Compliance
- **Data Encryption**: At rest (DynamoDB) and in transit (HTTPS)
- **Audit Logging**: CloudTrail integration
- **Access Logs**: API Gateway request logging

## Troubleshooting

### Common Issues

**ALB deployment taking too long:**
- ALB deployments typically take 3-5 minutes
- Check AWS console for load balancer status

**API Gateway domain not found:**
- Ensure serve-message-api is deployed first
- Check outputs: `terraform output api_gateway_domain_name`

**Custom domain not resolving:**
- DNS propagation can take 5-10 minutes
- Verify Route53 records point to ALB DNS name

**Lambda function errors:**
- Check CloudWatch logs: `/aws/lambda/serve-message-*`
- Verify environment variables are set correctly
- Ensure DynamoDB table exists and is accessible

### Useful Commands

```bash
# Check ALB status
aws elbv2 describe-load-balancers --names serve-message-alb-dev

# Test DNS resolution
dig ai-dev.goodparty.org

# Check API Gateway endpoints
curl -v https://abc123.execute-api.us-east-1.amazonaws.com/dev/health

# View Lambda logs
aws logs tail /aws/lambda/serve-message-set-dev --follow
```

## Future Enhancements

### Planned Additions
1. **ML Service**: `/ml/*` path for machine learning APIs
2. **Analytics Service**: `/analytics/*` path for data insights
3. **WebSocket Support**: Real-time features via API Gateway WebSocket
4. **Multi-Region**: Global deployment for better performance
5. **WAF Integration**: Web Application Firewall for enhanced security

### Infrastructure Improvements
1. **Blue/Green Deployments**: Zero-downtime deployments
2. **Auto Scaling**: DynamoDB auto-scaling based on usage
3. **Backup Strategy**: Automated DynamoDB backups
4. **Monitoring Dashboard**: CloudWatch dashboard for operational metrics
5. **CI/CD Pipeline**: Automated deployments via GitHub Actions

---

**🚀 Ready for Production!**

This infrastructure follows AWS Well-Architected principles and is designed to scale from prototype to enterprise workloads while maintaining cost efficiency and operational simplicity.