# Serve Messages API

Lambda functions for the Serve Messages API, providing campaign data storage and retrieval.

## Architecture

**Simplified Two-Tier Architecture**: Route53 → CloudFront → Lambda Function URLs

This architecture provides:
- **73% cost reduction** compared to three-tier (CloudFront → API Gateway → Lambda)
- **Faster deployments** (60 seconds vs 5-10 minutes)
- **Built-in security** via CloudFront Origin Access Control (OAC)
- **API key authentication** via CloudFront Functions

## API Endpoints

- **POST** `/serve/messages/{campaign_id}` - Store campaign data (IAM auth required)
- **GET** `/serve/messages/{campaign_id}` - Retrieve campaign data (API key required)

### Access URLs

- **Custom Domain**: `https://ai-dev.goodparty.org/serve/messages/{campaign_id}`
- **Direct Lambda URLs**: Protected by CloudFront OAC (not publicly accessible)

## Lambda Functions

### serve-message-set
- **Purpose**: Store campaign data via POST requests
- **Authentication**: AWS IAM (uses access key/secret)
- **Location**: `lambdas/serve-message-set/`

### serve-message-retrieve
- **Purpose**: Retrieve campaign data via GET requests
- **Authentication**: API Key in `x-api-key` header
- **Location**: `lambdas/serve-message-retrieve/`

## Development Workflow

### 1. Infrastructure Deployment (Rare)

Deploy infrastructure changes (new Lambda functions, Function URLs, CloudFront):

```bash
cd ../../infrastructure/serve-message-api/deploy/terraform/
./deploy.sh -e dev
```

**Note**: API Gateway module has been completely removed from infrastructure.

### 2. Code Deployment (Frequent)

Deploy Lambda function code changes only:

```bash
# Deploy all functions to dev
./quick-deploy.sh

# Deploy specific function
./deploy-lambdas.sh -e dev -f serve-message-set
./deploy-lambdas.sh -e dev -f serve-message-retrieve

# Deploy all functions with options
./deploy-lambdas.sh -e dev -a
./deploy-lambdas.sh -e prod -a
```

### 3. Testing

```bash
# Test GET with API key (YOUR_API_KEY_HERE for dev)
curl -X GET "https://ai-dev.goodparty.org/serve/messages/test-campaign" \
  -H "x-api-key: YOUR_API_KEY_HERE"

# Test POST (requires AWS credentials configured in environment)
curl -X POST "https://ai-dev.goodparty.org/serve/messages/test-campaign" \
  -H "Content-Type: application/json" \
  -d '{"message": "test data"}'

# Verify CloudFront security (should return 403)
curl -X GET "https://vyrskrcbwezxuqsnjuczpsh2oe0unnaq.lambda-url.us-west-2.on.aws/serve/messages/test-campaign"
```

## Environment Configuration

### API Keys and Authentication

- **GET requests**: API key `YOUR_API_KEY_HERE` (dev environment)
- **POST requests**: AWS IAM credentials (stored in `../../infrastructure/serve-message-api/terraform.tfvars`)

### Function URLs (Protected by CloudFront OAC)

- **SET Function**: `https://vyrskrcbwezxuqsnjuczpsh2oe0unnaq.lambda-url.us-west-2.on.aws/`
- **RETRIEVE Function**: `https://h7n6kjnrxh63g2uwmuokl5dbea0ziaub.lambda-url.us-west-2.on.aws/`

**Security Note**: Direct access to Function URLs returns 403 due to CloudFront Origin Access Control.

## Adding New Routes

### Option A: Add to existing Lambda functions
1. Edit code in `lambdas/serve-message-set/src/` or `lambdas/serve-message-retrieve/src/`
2. Run `./quick-deploy.sh`
3. No infrastructure changes needed
4. CloudFront automatically forwards to Function URLs

### Option B: Create new Lambda function
1. Create new directory under `lambdas/`
2. Add Terraform module with Function URL in `../../infrastructure/serve-message-api/deploy/terraform/`
3. Update CloudFront origin configuration if needed
4. Deploy infrastructure first, then code

## Performance Notes

- **Code deployments**: ~30-60 seconds (builds TypeScript, uploads ZIP)
- **Infrastructure deployments**: ~3-5 minutes (Lambda + CloudFront, no API Gateway)
- **Architecture benefit**: 73% cost reduction vs API Gateway approach
- **Prefer code deployments** for routine updates and new routes in existing functions

## CloudFront Configuration

The API uses CloudFront with:
- **Caching**: Disabled (`min_ttl = 0`, `default_ttl = 0`, `max_ttl = 0`)
- **Origin**: Lambda Function URLs with Origin Access Control (OAC)
- **Authentication**: CloudFront Function validates API keys for GET requests
- **Security**: Direct Function URL access blocked by OAC
- **Headers**: All headers forwarded to Lambda for proper authentication