#!/bin/bash

set -e

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}🔨 Building TypeScript Lambda Functions${NC}"

# Build SET Lambda
echo -e "${BLUE}Building SET Lambda...${NC}"
cd lambdas/set_campaign_data
npm run build
cd ../..

# Build RETRIEVE Lambda
echo -e "${BLUE}Building RETRIEVE Lambda...${NC}"
cd lambdas/retrieve_campaign_data
npm run build
cd ../..

echo -e "${GREEN}✅ All Lambda functions built successfully!${NC}"