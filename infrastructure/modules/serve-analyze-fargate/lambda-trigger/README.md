# Lambda Trigger Function

This Lambda function triggers the Step Functions state machine when new CSV files are uploaded to the S3 bucket.

## Build Process

### Prerequisites

- Node.js 18+ and npm
- TypeScript compiler

### Building the Deployment Package

Run the build script to compile TypeScript and create the deployment zip:

```bash
./build.sh
```

Or manually:

```bash
npm ci
npm run build
```

This will:
1. Install dependencies (including `@aws-sdk/client-sfn`)
2. Compile TypeScript (`index.ts` → `dist/index.js`)
3. Copy `node_modules` to `dist/`
4. Create `lambda-trigger.zip` containing the compiled code and dependencies

### Terraform Integration

The Terraform configuration references the built zip file:

```hcl
resource "aws_lambda_function" "pipeline_trigger" {
  filename         = "${path.module}/lambda-trigger/lambda-trigger.zip"
  source_code_hash = filebase64sha256("${path.module}/lambda-trigger/lambda-trigger.zip")
  # ...
}
```

The `source_code_hash` ensures Lambda updates when the code changes.

## Development

### Local Testing

You can test the Lambda function locally by creating a test event:

```javascript
const event = {
  Records: [
    {
      s3: {
        bucket: { name: "serve-analyze-data-dev" },
        object: { key: "input/test-campaign.csv" }
      }
    }
  ]
};
```

### Deployment

After making changes:

1. Build the package: `./build.sh`
2. Deploy with Terraform:
   ```bash
   cd ../../environments/dev/serve-analyze-fargate
   terraform plan
   terraform apply
   ```

## Lambda Configuration

- **Runtime**: Node.js 22.x
- **Handler**: `index.handler`
- **Timeout**: 60 seconds
- **Memory**: Default (128 MB)

## Environment Variables

The Lambda receives these environment variables from Terraform:

- `STATE_MACHINE_ARN` - ARN of the Step Functions state machine
- `ECS_CLUSTER_NAME` - Name of the ECS cluster
- `TASK_DEFINITION_ARN` - ARN of the ECS task definition
- `SUBNET_IDS` - Comma-separated list of subnet IDs
- `SECURITY_GROUP_ID` - Security group ID for ECS tasks
- `S3_OUTPUT_BUCKET` - S3 bucket for output files
- `SNS_TOPIC_ARN` - SNS topic ARN for failure notifications

## Troubleshooting

### Build fails with "Cannot find module"

Install dependencies first:
```bash
npm ci
```

### Lambda deployment fails

Ensure the zip file exists:
```bash
ls -lh lambda-trigger.zip
```

If missing, run `./build.sh`

### Lambda function not updating in AWS

Terraform uses `source_code_hash` to detect changes. If the hash hasn't changed, the Lambda won't update. Force an update:

```bash
terraform taint module.serve_analyze_fargate.aws_lambda_function.pipeline_trigger
terraform apply
```
