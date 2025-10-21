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
    echo ""
    echo "NOTE: You must deploy serve-message-api first to get the API Gateway domain,"
    echo "      then update environments/<env>.tfvars with the API Gateway domain."
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

echo -e "${BLUE}======================================${NC}"
if [[ "$DESTROY" == "true" ]]; then
    echo -e "${BLUE}🔥 DESTROYING Shared Infrastructure (ALB + Route53)${NC}"
else
    echo -e "${BLUE}🚀 DEPLOYING Shared Infrastructure (ALB + Route53)${NC}"
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

# Change to script directory
cd "$SCRIPT_DIR"

# Check if API Gateway domain is configured
if grep -q "REPLACE_WITH_API_GATEWAY_DOMAIN" "environments/${ENVIRONMENT}.tfvars"; then
    echo -e "${YELLOW}⚠️  WARNING: API Gateway domain not configured in environments/${ENVIRONMENT}.tfvars${NC}"
    echo -e "${YELLOW}   You need to deploy serve-message-api first and update the api_gateway_domain_name${NC}"
    if [[ "$PLAN_ONLY" != "true" ]]; then
        echo -e "${RED}   Exiting to prevent deployment with placeholder values${NC}"
        exit 1
    fi
fi

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
    echo -e "${YELLOW}⚠️  About to DESTROY shared infrastructure for ${ENVIRONMENT} environment${NC}"
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
    echo -e "${GREEN}✅ Shared infrastructure destroyed successfully!${NC}"
else
    echo -e "${YELLOW}⚠️  About to deploy shared infrastructure for ${ENVIRONMENT} environment${NC}"
    echo -e "${YELLOW}    This will create ALB (takes 3-5 minutes)${NC}"
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

    echo -e "${GREEN}✅ Shared infrastructure deployed successfully!${NC}"

    # Extract outputs
    CUSTOM_DOMAIN_URL=$(terraform output -raw custom_domain_url)
    ALB_DNS=$(terraform output -raw alb_dns_name)
    CUSTOM_DOMAIN_FQDN=$(terraform output -raw custom_domain_fqdn)

    # Display summary
    echo -e "${BLUE}======================================${NC}"
    echo -e "${BLUE}📊 SHARED INFRASTRUCTURE SUMMARY${NC}"
    echo -e "${BLUE}======================================${NC}"
    echo -e "${GREEN}Environment:${NC} $ENVIRONMENT"
    echo -e "${GREEN}Custom Domain:${NC} $CUSTOM_DOMAIN_URL"
    echo -e "${GREEN}ALB DNS:${NC} $ALB_DNS"
    echo -e "${GREEN}Domain FQDN:${NC} $CUSTOM_DOMAIN_FQDN"
    echo ""
    echo -e "${BLUE}🎯 NEXT STEPS:${NC}"
    echo -e "${GREEN}1.${NC} Wait for DNS propagation (5-10 minutes)"
    echo -e "${GREEN}2.${NC} Deploy serve-message-api services to this environment"
    echo -e "${GREEN}3.${NC} Test endpoints: $CUSTOM_DOMAIN_URL/serve/messages/..."
    echo -e "${BLUE}======================================${NC}"
fi

# Clean up
rm -f tfplan

echo -e "${GREEN}🎉 Shared infrastructure script completed successfully!${NC}"