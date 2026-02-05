#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../../.." && pwd)"
LAMBDA_DIR="$REPO_ROOT/clickup_bot/lambda"

FUNCTION_NAME="${1:-clickup-bot-prod}"
AWS_REGION="${AWS_REGION:-us-west-2}"

echo "Deploying Lambda: $FUNCTION_NAME"
echo "Source: $LAMBDA_DIR/handler.py"

cd "$LAMBDA_DIR"
zip -j /tmp/clickup-bot-lambda.zip handler.py

aws lambda update-function-code \
    --function-name "$FUNCTION_NAME" \
    --zip-file fileb:///tmp/clickup-bot-lambda.zip \
    --region "$AWS_REGION" \
    --output json | jq '{FunctionName, LastModified, CodeSha256}'

rm /tmp/clickup-bot-lambda.zip

echo "Deploy complete!"
