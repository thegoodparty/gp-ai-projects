#!/bin/bash

# Quick deploy script - deploys all Lambda functions to dev environment
# Usage: ./quick-deploy.sh
#
# Architecture: Route53 → CloudFront → Lambda Function URLs
# - No API Gateway (removed for 73% cost reduction)
# - CloudFront handles API key auth via CloudFront Functions
# - Lambda Function URLs protected by Origin Access Control (OAC)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "🚀 Quick deploying all Lambda functions to dev environment..."

# Use the main deploy script
"$SCRIPT_DIR/deploy-lambdas.sh" -e dev -a

echo "✅ Quick deploy complete!"