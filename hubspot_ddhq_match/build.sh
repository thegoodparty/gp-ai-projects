#!/bin/bash
set -e

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

LOCAL_IMAGE="ddhq-matcher"
TAG="${1:-dev}"

case "$TAG" in
  main|master|prod|qa|dev|release)
    ECR_TAG="ddhq-matcher-${TAG}"
    echo -e "${GREEN}Environment tag detected: ${YELLOW}${ECR_TAG}${NC} (never expires)${NC}"
    ;;
  v[0-9]*)
    ECR_TAG="ddhq-matcher-${TAG}"
    echo -e "${GREEN}Version tag detected: ${YELLOW}${ECR_TAG}${NC} (365 day retention)${NC}"
    ;;
  *)
    ECR_TAG="ddhq-matcher-${TAG}"
    echo -e "${BLUE}Project tag: ${YELLOW}${ECR_TAG}${NC} (180 day retention)${NC}"
    ;;
esac

if [ "$PUSH_TO_ECR" = "true" ]; then
  PLATFORM="${PLATFORM:-linux/arm64}"
  echo -e "${YELLOW}Building for ECR (production - Graviton ARM64): ${PLATFORM}${NC}"
else
  if [ -z "$PLATFORM" ]; then
    ARCH=$(uname -m)
    if [ "$ARCH" = "arm64" ] || [ "$ARCH" = "aarch64" ]; then
      PLATFORM="linux/arm64"
      echo -e "${BLUE}Auto-detected ARM architecture: ${YELLOW}${PLATFORM}${NC}"
    else
      PLATFORM="linux/amd64"
      echo -e "${BLUE}Auto-detected x86 architecture: ${YELLOW}${PLATFORM}${NC}"
    fi
  else
    echo -e "${YELLOW}Using specified platform: ${PLATFORM}${NC}"
  fi
fi

echo -e "${BLUE}Building DDHQ Matcher Docker Image${NC}"
echo -e "${GREEN}Platform: ${YELLOW}${PLATFORM}${NC}"
echo -e "${GREEN}Local tag: ${YELLOW}${TAG}${NC}"
echo -e "${GREEN}ECR tag: ${YELLOW}${ECR_TAG}${NC}"

cd "$(dirname "$0")/.."

if [ "$PUSH_TO_ECR" = "true" ]; then
  echo -e "${BLUE}Getting ECR repository URL...${NC}"

  ECR_REPO_URL=$(aws ecr describe-repositories \
    --repository-names gp-ai-projects \
    --query 'repositories[0].repositoryUri' \
    --output text)

  if [ -z "$ECR_REPO_URL" ]; then
    echo -e "${YELLOW}Warning: ECR repository 'gp-ai-projects' not found${NC}"
    echo -e "${YELLOW}Run: cd infrastructure/shared/ecr && terraform apply${NC}"
    exit 1
  fi

  echo -e "${BLUE}Logging in to ECR...${NC}"
  aws ecr get-login-password --region us-west-2 | \
    docker login --username AWS --password-stdin ${ECR_REPO_URL}

  echo -e "${BLUE}Building and pushing to ECR with buildx...${NC}"
  docker buildx build \
    --platform ${PLATFORM} \
    -f hubspot_ddhq_match/deployment/Dockerfile \
    -t ${ECR_REPO_URL}:${ECR_TAG} \
    -t ${ECR_REPO_URL}:ddhq-matcher-latest \
    --push \
    .

  echo -e "${GREEN}✓ Built and pushed to ECR!${NC}"
  echo -e "${GREEN}Image: ${ECR_REPO_URL}:${ECR_TAG}${NC}"
else
  echo -e "${BLUE}Building locally with buildx...${NC}"
  docker buildx build \
    --platform ${PLATFORM} \
    -f hubspot_ddhq_match/deployment/Dockerfile \
    -t ${LOCAL_IMAGE}:${TAG} \
    -t ${LOCAL_IMAGE}:latest \
    --load \
    .

  echo -e "${GREEN}✓ Build completed!${NC}"
  docker images | grep $LOCAL_IMAGE
fi
