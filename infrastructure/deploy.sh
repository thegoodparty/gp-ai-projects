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
SERVICE=""
AUTO_APPROVE=false
DESTROY=false
PLAN_ONLY=false

# Print usage
usage() {
    echo "Usage: $0 -e <environment> -s <service> [-a] [-d] [-p]"
    echo "  -e, --environment    Environment to deploy (dev/prod)"
    echo "  -s, --service        Service to deploy (serve-message-api, shared, all)"
    echo "  -a, --auto-approve   Auto approve terraform apply"
    echo "  -d, --destroy        Destroy infrastructure instead of deploy"
    echo "  -p, --plan-only      Run terraform plan only (no apply)"
    echo "  -h, --help           Show this help message"
    echo ""
    echo "Examples:"
    echo "  $0 -e dev -s serve-message-api -p    # Plan only for serve-message-api in dev"
    echo "  $0 -e dev -s all                     # Deploy all services to dev"
    echo "  $0 -e prod -s shared -a              # Deploy shared infrastructure to prod"
    echo "  $0 -e dev -s serve-message-api -d    # Destroy serve-message-api in dev"
    exit 1
}

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        -e|--environment)
            ENVIRONMENT="$2"
            shift 2
            ;;
        -s|--service)
            SERVICE="$2"
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

# Validate inputs
if [[ -z "$ENVIRONMENT" ]]; then
    echo -e "${RED}Error: Environment is required${NC}"
    usage
fi

if [[ -z "$SERVICE" ]]; then
    echo -e "${RED}Error: Service is required${NC}"
    usage
fi

if [[ "$ENVIRONMENT" != "dev" && "$ENVIRONMENT" != "prod" ]]; then
    echo -e "${RED}Error: Environment must be 'dev' or 'prod'${NC}"
    usage
fi

if [[ "$SERVICE" != "serve-message-api" && "$SERVICE" != "shared" && "$SERVICE" != "all" ]]; then
    echo -e "${RED}Error: Service must be 'serve-message-api', 'shared', or 'all'${NC}"
    usage
fi

# Get script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo -e "${BLUE}======================================${NC}"
if [[ "$DESTROY" == "true" ]]; then
    echo -e "${BLUE}🔥 DESTROYING Infrastructure${NC}"
else
    echo -e "${BLUE}🚀 DEPLOYING Infrastructure${NC}"
fi
echo -e "${BLUE}Environment: ${YELLOW}$ENVIRONMENT${NC}"
echo -e "${BLUE}Service: ${YELLOW}$SERVICE${NC}"
echo -e "${BLUE}======================================${NC}"

# Check prerequisites
if ! command -v terraform &> /dev/null; then
    echo -e "${RED}Error: Terraform is not installed${NC}"
    exit 1
fi

# Set AWS profile to work
export AWS_PROFILE=work
echo -e "${GREEN}Using AWS Profile: ${YELLOW}work${NC}"

if ! aws sts get-caller-identity &> /dev/null; then
    echo -e "${RED}Error: AWS CLI is not configured or credentials are invalid${NC}"
    exit 1
fi

echo -e "${GREEN}✓ Prerequisites check passed${NC}"

# Function to deploy a specific service
deploy_service() {
    local service_name=$1
    local service_dir="$SCRIPT_DIR/$service_name"

    if [[ ! -d "$service_dir" ]]; then
        echo -e "${RED}Error: Service directory not found: $service_dir${NC}"
        return 1
    fi

    echo -e "${BLUE}📦 Deploying service: ${YELLOW}$service_name${NC}"

    # For serve-message-api, use the existing deploy script
    if [[ "$service_name" == "serve-message-api" ]]; then
        local deploy_script="$service_dir/deploy/scripts/deploy.sh"
        if [[ -f "$deploy_script" ]]; then
            local args="-e $ENVIRONMENT"
            if [[ "$AUTO_APPROVE" == "true" ]]; then
                args="$args -a"
            fi
            if [[ "$DESTROY" == "true" ]]; then
                args="$args -d"
            fi
            if [[ "$PLAN_ONLY" == "true" ]]; then
                args="$args -p"
            fi

            echo -e "${BLUE}Executing: $deploy_script $args${NC}"
            bash "$deploy_script" $args
        else
            echo -e "${RED}Error: Deploy script not found: $deploy_script${NC}"
            return 1
        fi
    else
        echo -e "${YELLOW}Service $service_name deployment not yet implemented${NC}"
    fi
}

# Deploy based on service selection
case $SERVICE in
    "serve-message-api")
        deploy_service "serve-message-api"
        ;;
    "shared")
        echo -e "${YELLOW}Shared infrastructure deployment not yet implemented${NC}"
        echo -e "${BLUE}Shared infrastructure includes: ALB, Route53${NC}"
        ;;
    "all")
        echo -e "${BLUE}Deploying all services...${NC}"
        deploy_service "serve-message-api"
        echo -e "${YELLOW}Shared infrastructure deployment will be added next${NC}"
        ;;
    *)
        echo -e "${RED}Unknown service: $SERVICE${NC}"
        exit 1
        ;;
esac

echo -e "${GREEN}🎉 Infrastructure deployment script completed!${NC}"