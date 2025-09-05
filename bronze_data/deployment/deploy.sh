#!/bin/bash
set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}🚀 LLM Matching Service Deployment${NC}"
echo -e "${BLUE}=====================================${NC}"

# Check prerequisites
echo -e "${YELLOW}🔍 Checking prerequisites...${NC}"

# Check if we're in the right directory
if [ ! -f "template.yaml" ]; then
    echo -e "${RED}❌ Error: template.yaml not found. Please run from the deployment directory.${NC}"
    exit 1
fi

# Check UV
if ! command -v uv &> /dev/null; then
    echo -e "${RED}❌ Error: uv not found. Please install uv first.${NC}"
    exit 1
fi

# Check AWS CLI
if ! command -v aws &> /dev/null; then
    echo -e "${RED}❌ Error: AWS CLI not found. Please install AWS CLI first.${NC}"
    exit 1
fi

# Check SAM CLI
if ! command -v sam &> /dev/null; then
    echo -e "${RED}❌ Error: SAM CLI not found. Please install SAM CLI first.${NC}"
    exit 1
fi

# Check AWS credentials
if ! aws sts get-caller-identity &> /dev/null; then
    echo -e "${RED}❌ Error: AWS credentials not configured. Run 'aws configure' first.${NC}"
    exit 1
fi

echo -e "${GREEN}✅ Prerequisites check passed${NC}"

# Copy shared folder from project root
echo -e "${YELLOW}📁 Copying shared modules...${NC}"
if [ -d "../../shared" ]; then
    rm -rf shared  # Remove existing shared folder if present
    cp -r ../../shared ./shared
    echo -e "${GREEN}✅ Shared modules copied${NC}"
else
    echo -e "${RED}❌ Error: Shared folder not found at ../../shared${NC}"
    exit 1
fi

# Check if requirements.txt already exists and is clean
if [ -f "requirements.txt" ] && ! grep -q "^-e " requirements.txt; then
    echo -e "${GREEN}✅ Clean requirements.txt already exists, skipping export${NC}"
else
    # Export UV dependencies
    echo -e "${YELLOW}📦 Exporting dependencies from UV...${NC}"
    cd ../../
    if ! uv export --format requirements-txt > bronze_data/deployment/requirements.txt; then
        echo -e "${RED}❌ Error: Failed to export UV dependencies${NC}"
        exit 1
    fi
    cd bronze_data/deployment
    echo -e "${GREEN}✅ Dependencies exported to requirements.txt${NC}"
fi

# Build SAM application
echo -e "${YELLOW}🏗️  Building SAM application...${NC}"
if ! sam build; then
    echo -e "${RED}❌ Error: SAM build failed${NC}"
    exit 1
fi
echo -e "${GREEN}✅ SAM build completed${NC}"

# Load environment variables from .env file if it exists
ENV_FILE="../../.env"
if [ -f "$ENV_FILE" ]; then
    echo -e "${YELLOW}📄 Loading environment variables from .env file...${NC}"
    # Load .env file, filtering out comments and empty lines
    export $(grep -v '^#' "$ENV_FILE" | grep -v '^$' | xargs)
    echo -e "${GREEN}✅ Environment variables loaded${NC}"
else
    echo -e "${YELLOW}💡 No .env file found at project root. Using manual input...${NC}"
fi

# Set Lambda API key from environment
if [ -z "$LAMBDA_API_KEY" ]; then
    echo -e "${RED}❌ Error: LAMBDA_API_KEY not found in environment variables${NC}"
    echo -e "${YELLOW}💡 Please add LAMBDA_API_KEY to your .env file at project root${NC}"
    exit 1
fi

if [ -z "$GEMINI_API_KEY" ]; then
    echo -e "${YELLOW}Please provide your Gemini API key:${NC}"
    read -s GEMINI_API_KEY
fi

# Deploy
echo -e "${YELLOW}🚀 Deploying to AWS...${NC}"
PARAM_OVERRIDES="ApiKey=${LAMBDA_API_KEY} GeminiApiKey=${GEMINI_API_KEY} S3BucketName=llm-matching-embeddings"

if [ "$1" == "--guided" ] || [ ! -f "samconfig.toml" ]; then
    echo -e "${BLUE}Running guided deployment (first time setup)...${NC}"
    if ! sam deploy --guided --parameter-overrides "$PARAM_OVERRIDES"; then
        echo -e "${RED}❌ Error: SAM deployment failed${NC}"
        exit 1
    fi
else
    echo -e "${BLUE}Using existing configuration...${NC}"
    if ! sam deploy --resolve-s3 --stack-name llm-matching --parameter-overrides "$PARAM_OVERRIDES"; then
        echo -e "${RED}❌ Error: SAM deployment failed${NC}"
        exit 1
    fi
fi

# Get outputs
echo -e "${YELLOW}📋 Deployment outputs:${NC}"
sam list stack-outputs --stack-name llm-matching --output table

echo -e "${GREEN}🎉 Deployment completed successfully!${NC}"
echo -e "${BLUE}📝 Next steps:${NC}"
echo -e "   1. Upload embeddings to S3 using: ${YELLOW}./deploy_data.sh${NC}"
echo -e "   2. Test the API using the Function URL from outputs above"
echo -e "${BLUE}📋 API Access Information:${NC}"
echo -e "   • API Key: ${GREEN}${LAMBDA_API_KEY}${NC}"
echo -e "   • Header: ${YELLOW}x-api-key: ${LAMBDA_API_KEY}${NC}"
echo -e "${YELLOW}💡 Save the API key above - you'll need it for all API requests${NC}"
echo -e "${RED}⚠️  SECURITY: Do not commit this API key to version control!${NC}"