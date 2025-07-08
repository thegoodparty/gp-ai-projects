#!/bin/bash

# Configuration
FUNCTION_NAME="campaign-plan-generator"
REGION="us-east-1"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
IMAGE_URI="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${FUNCTION_NAME}:latest"

# Create ECR repository if it doesn't exist
aws ecr create-repository --repository-name $FUNCTION_NAME --region $REGION 2>/dev/null || true

# Get login token for ECR
aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin $IMAGE_URI

# Build and push Docker image
echo "Building Docker image..."
docker build -t $FUNCTION_NAME -f Dockerfile.lambda .
docker tag $FUNCTION_NAME:latest $IMAGE_URI
docker push $IMAGE_URI

# Create or update Lambda function
echo "Deploying Lambda function..."
aws lambda get-function --function-name $FUNCTION_NAME --region $REGION 2>/dev/null

if [ $? -eq 0 ]; then
    echo "Updating existing function..."
    aws lambda update-function-code \
        --function-name $FUNCTION_NAME \
        --image-uri $IMAGE_URI \
        --region $REGION
else
    echo "Creating new function..."
    aws lambda create-function \
        --function-name $FUNCTION_NAME \
        --package-type Image \
        --code ImageUri=$IMAGE_URI \
        --role arn:aws:iam::$ACCOUNT_ID:role/lambda-execution-role \
        --timeout 300 \
        --memory-size 1024 \
        --region $REGION \
        --environment Variables='{
            "GEMINI_API_KEY":"'$GEMINI_API_KEY'",
            "TOGETHER_API_KEY":"'$TOGETHER_API_KEY'",
            "TAVILY_API_KEY":"'$TAVILY_API_KEY'"
        }'
fi

# Update function configuration
aws lambda update-function-configuration \
    --function-name $FUNCTION_NAME \
    --timeout 300 \
    --memory-size 1024 \
    --region $REGION

# Create or update API Gateway (optional)
echo "Creating API Gateway..."
API_ID=$(aws apigateway create-rest-api --name $FUNCTION_NAME --region $REGION --query 'id' --output text 2>/dev/null)

if [ $? -eq 0 ]; then
    echo "API Gateway created with ID: $API_ID"
    echo "You can access your API at: https://$API_ID.execute-api.$REGION.amazonaws.com/prod/"
else
    echo "API Gateway creation failed or already exists"
fi

echo "Deployment complete!"
echo "Function ARN: arn:aws:lambda:$REGION:$ACCOUNT_ID:function:$FUNCTION_NAME" 