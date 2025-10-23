# Serve Messages API - Architecture Overview

## Multi-Service ALB Architecture

This service is one of multiple services sharing a common Application Load Balancer:

```
                Route53 (ai-dev.goodparty.org)
                           ↓
              Application Load Balancer (Shared)
                           ↓
        ┌──────────────────┼──────────────────┐
        │                  │                  │
  /serve/messages/*      /ml/*         /analytics/*
        ↓                  ↓                  ↓
   serve-message      ml-inference       analytics
   (this service)       (future)          (future)
```

**Route53 → ALB → Lambda Target Groups → DynamoDB**

### Architecture Benefits

- **Multi-Service Design**: Single ALB routes to multiple services via path patterns
- **Resource Efficient**: One ALB serves unlimited services
- **Scalable**: Easy to add new services without infrastructure duplication
- **Direct Lambda Integration**: ALB target groups (no API Gateway)
- **Immediate Deployments**: No CDN propagation delays
- **Built-In Security**: API key validation at ALB listener level
- **Unified Authentication**: Same API key across all services
- **Health Check Monitoring**: Disabled for Lambda (on-demand invocation)

### Components

#### 1. Route53
- DNS routing for custom domain `ai-dev.goodparty.org`
- A record points to ALB DNS name
- Hosted zone: `goodparty.org`

#### 2. Application Load Balancer (ALB)
- **Custom Domain**: `https://ai-dev.goodparty.org/serve/messages/{campaign_id}`
- **Target Groups**: Lambda function target group with health checks
- **Authentication**: ALB listener rules validate `x-api-key` header
- **Health Checks**: `/serve/messages/health` endpoint (no API key required)
- **Security**: API key validation at listener rule level - invalid requests never reach Lambda
- **Listener Rules**:
  - Priority 10: Health check endpoint (no auth)
  - Priority 20: Valid API key → forward to Lambda
  - Priority 30: Invalid/missing API key → return 403
  - Default: Return 404 for unmatched paths

#### 3. Lambda Function
- **Unified Function**: `serve-message-dev` handles both GET and POST
- **Integration**: Direct ALB target group attachment via `aws_lb_target_group_attachment`
- **HTTP Method Routing**: Single handler branches on `httpMethod` property
- **Health Check**: Responds to `/serve/messages/health` for ALB monitoring
- **CORS**: Configured for cross-origin requests
- **Runtime**: Node.js 22.x with TypeScript compilation

#### 4. DynamoDB
- **Table**: `serve-messages-dev`
- **Design**: Single table with `campaign_id` as partition key, `record_id` as sort key
- **Billing**: Pay-per-request (no provisioned capacity)
- **Access**: Direct from Lambda functions using IAM roles with least-privilege policies

### Request Flow

#### GET/POST Requests with API Key
1. Client sends HTTPS request to `https://ai-dev.goodparty.org/serve/messages/{campaign_id}`
2. Route53 resolves to ALB DNS name
3. ALB HTTPS listener receives request on port 443
4. ALB listener rule checks for `x-api-key` header
5. **If valid key**: Forward to Lambda target group
6. **If invalid/missing key**: Return 403 Forbidden (request never reaches Lambda)
7. Lambda processes request (GET retrieval or POST storage) with DynamoDB
8. Lambda returns response to ALB
9. ALB returns response to client

#### Health Check Flow
1. ALB sends periodic health checks to `/serve/messages/health`
2. Health check matches Priority 10 listener rule (no API key required)
3. Lambda returns 200 status with health information
4. ALB marks target as healthy/unhealthy based on response

#### Authentication Enforcement
- **ALB Level**: Listener rules block unauthorized requests before Lambda invocation
- **Cost Benefit**: Invalid requests don't trigger Lambda execution (no cost)
- **Security**: API key never reaches Lambda (validated at load balancer)

### Security Features

- **ALB Listener Rules**: Prevent access without valid API key
- **API Key Validation**: ALB blocks unauthorized requests at load balancer level
- **Target Group Security**: Lambda only accessible through ALB
- **CORS**: Properly configured for browser-based requests
- **HTTPS Only**: All traffic encrypted in transit with SSL termination at ALB

### Deployment Architecture

```
Infrastructure (Terraform):
├── shared/alb/                 # Application Load Balancer + auth rules
├── serve-message-api/deploy/   # Lambda functions + DynamoDB + IAM
└── shared/route53/            # DNS configuration

Application Code:
└── serve/messages/lambdas/    # TypeScript Lambda functions
```

### Previous Architectures

**v1 (Original)**: Route53 → CloudFront → API Gateway → Lambda
- Used API Gateway for routing
- CloudFront for custom domain
- Complex three-tier architecture

**v2 (Attempted)**: Route53 → CloudFront → Lambda Function URLs
- Removed API Gateway
- Used Lambda Function URLs with CloudFront
- CloudFront Function for API key validation
- 15-20 minute CloudFront propagation delays

**v3 (Current)**: Route53 → ALB → Lambda Target Groups
- Removed CloudFront entirely
- Direct ALB to Lambda integration
- API key validation at ALB listener rules
- Immediate deployments (no CDN propagation)
- Lower latency


### Performance Characteristics

- **Latency**: Sub-100ms for typical requests
- **Throughput**: 1000+ concurrent requests supported
- **Scalability**: Auto-scaling with AWS Lambda concurrency
- **Deployment**: 60-second code deployments vs 5-10 minute infrastructure deployments