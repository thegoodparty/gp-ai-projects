#!/bin/bash

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Default values
ENVIRONMENT=""
AUTO_APPROVE=false
DESTROY=false
PLAN_ONLY=false

# Print usage
usage() {
    echo "Usage: $0 -e <environment> [-a] [-d] [-p]"
    echo "  -e, --environment    Environment to deploy (dev/prod)"
    echo "  -a, --auto-approve   Auto approve terraform apply"
    echo "  -d, --destroy        Destroy infrastructure instead of deploy"
    echo "  -p, --plan-only      Run terraform plan only (no apply)"
    echo "  -h, --help           Show this help message"
    echo ""
    echo "Examples:"
    echo "  $0 -e dev -p                 # Plan only for dev environment"
    echo "  $0 -e dev                    # Deploy to dev environment"
    echo "  $0 -e prod -a                # Deploy to prod with auto-approve"
    echo "  $0 -e dev -d                 # Destroy dev environment"
    exit 1
}

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        -e|--environment)
            ENVIRONMENT="$2"
            shift 2
            ;;
        -a|--auto-approve)
            AUTO_APPROVE=true
            shift
            ;;
        -d|--destroy)
            DESTROY=true
            shift
            ;;
        -p|--plan-only)
            PLAN_ONLY=true
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

# Get script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TERRAFORM_DIR="$SCRIPT_DIR/../terraform"

echo -e "${BLUE}======================================${NC}"
if [[ "$DESTROY" == "true" ]]; then
    echo -e "${BLUE}🔥 DESTROYING Serve Message Infrastructure${NC}"
else
    echo -e "${BLUE}🚀 DEPLOYING Serve Message Infrastructure${NC}"
fi
echo -e "${BLUE}Environment: ${YELLOW}$ENVIRONMENT${NC}"
echo -e "${BLUE}======================================${NC}"

# Check if terraform is installed
if ! command -v terraform &> /dev/null; then
    echo -e "${RED}Error: Terraform is not installed${NC}"
    exit 1
fi

# Set AWS profile to work
export AWS_PROFILE=work
echo -e "${GREEN}Using AWS Profile: ${YELLOW}work${NC}"

# Check if AWS CLI is configured
if ! aws sts get-caller-identity &> /dev/null; then
    echo -e "${RED}Error: AWS CLI is not configured or credentials are invalid${NC}"
    exit 1
fi

echo -e "${GREEN}✓ Prerequisites check passed${NC}"

# Change to terraform directory
cd "$TERRAFORM_DIR"

# Initialize Terraform
echo -e "${BLUE}📦 Initializing Terraform...${NC}"
terraform init

# Validate Terraform configuration
echo -e "${BLUE}🔍 Validating Terraform configuration...${NC}"
terraform validate

# Plan Terraform deployment
echo -e "${BLUE}📋 Planning Terraform deployment...${NC}"
if [[ "$DESTROY" == "true" ]]; then
    terraform plan -destroy -var-file="environments/${ENVIRONMENT}.tfvars" -out="tfplan"
else
    terraform plan -var-file="environments/${ENVIRONMENT}.tfvars" -out="tfplan"
fi

# If plan-only, exit here
if [[ "$PLAN_ONLY" == "true" ]]; then
    echo -e "${GREEN}✅ Plan completed successfully! Review the plan above.${NC}"
    echo -e "${BLUE}To apply this plan, run: ./deploy.sh -e ${ENVIRONMENT}${NC}"
    rm -f tfplan
    exit 0
fi

# Apply or Destroy
if [[ "$DESTROY" == "true" ]]; then
    echo -e "${YELLOW}⚠️  About to DESTROY infrastructure for ${ENVIRONMENT} environment${NC}"
    if [[ "$AUTO_APPROVE" == "true" ]]; then
        terraform apply -auto-approve "tfplan"
    else
        echo -e "${YELLOW}Do you want to continue? (yes/no)${NC}"
        read -r response
        if [[ "$response" == "yes" ]]; then
            terraform apply "tfplan"
        else
            echo -e "${RED}Deployment cancelled${NC}"
            exit 1
        fi
    fi
    echo -e "${GREEN}✅ Infrastructure destroyed successfully!${NC}"
else
    echo -e "${YELLOW}⚠️  About to deploy infrastructure for ${ENVIRONMENT} environment${NC}"
    if [[ "$AUTO_APPROVE" == "true" ]]; then
        terraform apply -auto-approve "tfplan"
    else
        echo -e "${YELLOW}Do you want to continue? (yes/no)${NC}"
        read -r response
        if [[ "$response" == "yes" ]]; then
            terraform apply "tfplan"
        else
            echo -e "${RED}Deployment cancelled${NC}"
            exit 1
        fi
    fi

    echo -e "${GREEN}✅ Deployment completed successfully!${NC}"

    # Generate .env configuration
    echo -e "${BLUE}📝 Generating environment configuration...${NC}"

    # Create env configuration file
    ENV_FILE="$SCRIPT_DIR/../../../.env.${ENVIRONMENT}"

    echo "# Serve Message Platform - ${ENVIRONMENT} Environment" > "$ENV_FILE"
    echo "# Generated on $(date)" >> "$ENV_FILE"
    echo "" >> "$ENV_FILE"

    # Extract outputs
    TABLE_NAME=$(terraform output -raw dynamodb_table_name)
    SET_ACCESS_KEY=$(terraform output -raw set_lambda_user_access_key_id)
    SET_SECRET_KEY=$(terraform output -raw set_lambda_user_secret_access_key)
    API_URL=$(terraform output -raw api_gateway_url)
    API_KEY=$(terraform output -raw retrieve_api_key_value)
    CUSTOM_DOMAIN_URL=$(terraform output -raw custom_domain_url)

    # Write to env file
    echo "# DynamoDB" >> "$ENV_FILE"
    echo "${ENVIRONMENT^^}_TABLE_NAME=$TABLE_NAME" >> "$ENV_FILE"
    echo "" >> "$ENV_FILE"
    echo "# SET Lambda (Programmatic Access)" >> "$ENV_FILE"
    echo "${ENVIRONMENT^^}_SET_LAMBDA_ACCESS_KEY_ID=$SET_ACCESS_KEY" >> "$ENV_FILE"
    echo "${ENVIRONMENT^^}_SET_LAMBDA_SECRET_ACCESS_KEY=$SET_SECRET_KEY" >> "$ENV_FILE"
    echo "" >> "$ENV_FILE"
    echo "# RETRIEVE API (Custom Domain)" >> "$ENV_FILE"
    echo "${ENVIRONMENT^^}_RETRIEVE_API_URL=$CUSTOM_DOMAIN_URL" >> "$ENV_FILE"
    echo "${ENVIRONMENT^^}_RETRIEVE_API_KEY=$API_KEY" >> "$ENV_FILE"

    echo -e "${GREEN}✅ Environment configuration saved to: ${ENV_FILE}${NC}"

    # Display summary
    echo -e "${BLUE}======================================${NC}"
    echo -e "${BLUE}📊 DEPLOYMENT SUMMARY${NC}"
    echo -e "${BLUE}======================================${NC}"
    echo -e "${GREEN}Environment:${NC} $ENVIRONMENT"
    echo -e "${GREEN}DynamoDB Table:${NC} $TABLE_NAME"
    echo -e "${GREEN}Custom Domain:${NC} $CUSTOM_DOMAIN_URL"
    echo ""
    echo -e "${BLUE}🚀 API ENDPOINTS:${NC}"
    echo -e "${GREEN}SET Endpoint:${NC} POST $CUSTOM_DOMAIN_URL/serve/messages/{campaign_id}/data"
    echo -e "${GREEN}RETRIEVE Endpoint:${NC} GET $CUSTOM_DOMAIN_URL/serve/messages/{campaign_id}/data"
    echo ""
    echo -e "${BLUE}📋 LEGACY ENDPOINTS (API Gateway Direct):${NC}"
    echo -e "${GREEN}SET Endpoint:${NC} POST $API_URL/campaigns/{campaign_id}/data"
    echo -e "${GREEN}RETRIEVE Endpoint:${NC} GET $API_URL/campaigns/{campaign_id}/data"
    echo ""
    echo -e "${GREEN}Environment File:${NC} $ENV_FILE"
    echo -e "${BLUE}======================================${NC}"
fi

# Clean up
rm -f tfplan

echo -e "${GREEN}🎉 Script completed successfully!${NC}"