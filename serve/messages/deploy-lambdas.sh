#!/bin/bash
#
# Lambda deployment script for Serve Messages API
# Architecture: Route53 → CloudFront → Lambda Function URLs
# - No API Gateway (removed for 73% cost reduction)
# - CloudFront handles API key auth via CloudFront Functions
# - Lambda Function URLs protected by Origin Access Control (OAC)

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Default values
ENVIRONMENT=""
FUNCTION_NAME=""
ALL_FUNCTIONS=false

# Print usage
usage() {
    echo "Usage: $0 -e <environment> [-f <function>] [-a]"
    echo "  -e, --environment    Environment (dev/prod)"
    echo "  -f, --function       Specific function to deploy (serve-message-set|serve-message-retrieve|serve-message)"
    echo "  -a, --all           Deploy all functions"
    echo "  -h, --help          Show this help message"
    echo ""
    echo "Examples:"
    echo "  $0 -e dev -f serve-message-set     # Deploy only the set function"
    echo "  $0 -e dev -f serve-message         # Deploy unified serve-message function"
    echo "  $0 -e dev -a                      # Deploy all functions"
    echo "  $0 -e prod -f serve-message-retrieve # Deploy retrieve function to prod"
    exit 1
}

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        -e|--environment)
            ENVIRONMENT="$2"
            shift 2
            ;;
        -f|--function)
            FUNCTION_NAME="$2"
            shift 2
            ;;
        -a|--all)
            ALL_FUNCTIONS=true
            shift
            ;;
        -h|--help)
            usage
            ;;
        *)
            echo "Unknown option $1"
            usage
            ;;
    esac
done

# Validate environment
if [[ -z "$ENVIRONMENT" ]]; then
    echo -e "${RED}Error: Environment is required${NC}"
    usage
fi

if [[ "$ENVIRONMENT" != "dev" && "$ENVIRONMENT" != "prod" ]]; then
    echo -e "${RED}Error: Environment must be 'dev' or 'prod'${NC}"
    usage
fi

# Validate function selection
if [[ "$ALL_FUNCTIONS" == "false" && -z "$FUNCTION_NAME" ]]; then
    echo -e "${RED}Error: Must specify either -f <function> or -a (all)${NC}"
    usage
fi

if [[ -n "$FUNCTION_NAME" && "$FUNCTION_NAME" != "serve-message-set" && "$FUNCTION_NAME" != "serve-message-retrieve" && "$FUNCTION_NAME" != "serve-message" ]]; then
    echo -e "${RED}Error: Function must be 'serve-message-set', 'serve-message-retrieve', or 'serve-message'${NC}"
    usage
fi

# Set AWS profile
export AWS_PROFILE=work
echo -e "${GREEN}Using AWS Profile: ${YELLOW}work${NC}"

# Check if AWS CLI is configured
if ! aws sts get-caller-identity &> /dev/null; then
    echo -e "${RED}Error: AWS CLI is not configured or credentials are invalid${NC}"
    exit 1
fi

# Get script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo -e "${BLUE}======================================${NC}"
echo -e "${BLUE}🚀 DEPLOYING Lambda Functions${NC}"
echo -e "${BLUE}Environment: ${YELLOW}$ENVIRONMENT${NC}"
if [[ "$ALL_FUNCTIONS" == "true" ]]; then
    echo -e "${BLUE}Functions: ${YELLOW}ALL${NC}"
else
    echo -e "${BLUE}Function: ${YELLOW}$FUNCTION_NAME${NC}"
fi
echo -e "${BLUE}======================================${NC}"

# Function to deploy a single Lambda
deploy_lambda() {
    local func_name=$1
    local lambda_dir="$SCRIPT_DIR/lambdas/$func_name"
    local aws_function_name="$func_name-$ENVIRONMENT"

    echo -e "${BLUE}📦 Deploying $func_name...${NC}"

    # Check if directory exists
    if [[ ! -d "$lambda_dir" ]]; then
        echo -e "${RED}Error: Lambda directory not found: $lambda_dir${NC}"
        return 1
    fi

    # Change to lambda directory
    cd "$lambda_dir"

    # Install dependencies and build
    echo -e "${YELLOW}  Installing dependencies...${NC}"
    npm ci

    echo -e "${YELLOW}  Building TypeScript...${NC}"
    npm run build

    # Create deployment package
    echo -e "${YELLOW}  Creating deployment package...${NC}"
    rm -f function.zip
    # Copy dist files to root level and include node_modules
    cp -r dist/* .
    zip -r function.zip *.js *.d.ts node_modules/ -x "*.map" "node_modules/.cache/*"
    # Clean up copied files
    rm -f *.js *.d.ts

    # Update Lambda function code
    echo -e "${YELLOW}  Updating Lambda function: $aws_function_name${NC}"
    aws lambda update-function-code \
        --function-name "$aws_function_name" \
        --zip-file fileb://function.zip \
        --region us-west-2

    # Wait for update to complete
    echo -e "${YELLOW}  Waiting for function update to complete...${NC}"
    aws lambda wait function-updated \
        --function-name "$aws_function_name" \
        --region us-west-2

    # Clean up
    rm -f function.zip

    echo -e "${GREEN}✅ Successfully deployed $func_name${NC}"

    # Return to script directory
    cd "$SCRIPT_DIR"
}

# Deploy functions
if [[ "$ALL_FUNCTIONS" == "true" ]]; then
    deploy_lambda "serve-message-set"
    deploy_lambda "serve-message-retrieve"
else
    deploy_lambda "$FUNCTION_NAME"
fi

echo -e "${BLUE}======================================${NC}"
echo -e "${GREEN}🎉 Lambda deployment completed successfully!${NC}"
echo -e "${BLUE}======================================${NC}"

# Show function info
echo -e "${BLUE}📊 DEPLOYMENT SUMMARY${NC}"
if [[ "$ALL_FUNCTIONS" == "true" ]]; then
    for func in "serve-message-set" "serve-message-retrieve"; do
        aws_func="$func-$ENVIRONMENT"
        last_modified=$(aws lambda get-function --function-name "$aws_func" --region us-west-2 --query 'Configuration.LastModified' --output text)
        echo -e "${GREEN}$aws_func:${NC} Updated at $last_modified"
    done
else
    aws_func="$FUNCTION_NAME-$ENVIRONMENT"
    last_modified=$(aws lambda get-function --function-name "$aws_func" --region us-west-2 --query 'Configuration.LastModified' --output text)
    echo -e "${GREEN}$aws_func:${NC} Updated at $last_modified"
fi

echo ""
echo -e "${BLUE}🔗 Test your deployment:${NC}"
echo -e "${GREEN}Custom Domain:${NC} https://ai-dev.goodparty.org/serve/messages/{campaign_id}"
echo -e "${GREEN}Direct API:${NC} https://yo2yfiwxhj.execute-api.us-west-2.amazonaws.com/dev/serve/messages/{campaign_id}"