#!/bin/bash
set -e

echo "🚀 Deploying Slack Notifier for ECS Task Failures"
echo "=================================================="

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo ""
echo "Step 1: Building Slack Notifier Lambda..."
cd "$SCRIPT_DIR/slack-notifier"

if [ ! -d "node_modules" ]; then
  echo "Installing dependencies..."
  npm install
fi

echo "Compiling TypeScript..."
npm run build

echo "Creating deployment package..."
cd dist
rm -f ../slack-notifier.zip
zip -r ../slack-notifier.zip .

echo "Moving package to module root..."
cd ..
mv slack-notifier.zip "$SCRIPT_DIR/"

echo "✅ Lambda package created: slack-notifier.zip"
echo ""

echo "Step 2: Checking for Lambda trigger updates..."
cd "$SCRIPT_DIR/lambda-trigger"

if [ ! -d "node_modules" ]; then
  echo "Installing dependencies..."
  npm install
fi

echo "Compiling TypeScript..."
npm run build

echo "Creating deployment package..."
cd dist
rm -f ../lambda-trigger.zip
zip -r ../lambda-trigger.zip .

echo "Moving package to module root..."
cd ..
mv lambda-trigger.zip "$SCRIPT_DIR/"

echo "✅ Lambda trigger package updated: lambda-trigger.zip"
echo ""

echo "=================================================="
echo "✅ Build Complete!"
echo ""
echo "Next steps:"
echo "1. Set your Slack webhook URL:"
echo "   export TF_VAR_slack_webhook_url='YOUR_WEBHOOK_URL'"
echo ""
echo "2. Navigate to environment directory:"
echo "   cd infrastructure/environments/dev/serve-analyze-fargate"
echo ""
echo "3. Deploy with Terraform:"
echo "   terraform init"
echo "   terraform plan"
echo "   terraform apply"
echo ""
echo "4. Test the integration:"
echo "   echo 'invalid,data' | aws s3 cp - s3://serve-analyze-data-dev/input/test-failure.csv"
echo ""
echo "Check your Slack channel in ~60 seconds! 🎉"
