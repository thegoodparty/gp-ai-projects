# Production Deployment Checklist

Follow this checklist to deploy serve-analyze-fargate to production.

## Pre-Deployment

- [ ] **Build and push Docker image**
  ```bash
  cd /Users/collinpark/work/gp-ai-projects
  docker buildx build --platform linux/arm64 -t serve-analyze-prod -f serve/v1_pipeline/Dockerfile .
  docker tag serve-analyze-prod:latest 333022194791.dkr.ecr.us-west-2.amazonaws.com/gp-ai-projects:serve-analyze-prod
  AWS_PROFILE=work aws ecr get-login-password --region us-west-2 | docker login --username AWS --password-stdin 333022194791.dkr.ecr.us-west-2.amazonaws.com
  docker push 333022194791.dkr.ecr.us-west-2.amazonaws.com/gp-ai-projects:serve-analyze-prod
  ```

- [ ] **Update terraform.tfvars with production API keys**
  ```bash
  cd infrastructure/environments/prod/serve-analyze-fargate
  # Edit terraform.tfvars and replace placeholders
  vi terraform.tfvars
  ```

- [ ] **Verify DynamoDB table exists**
  ```bash
  AWS_PROFILE=work aws dynamodb describe-table --table-name serve-message-v1-prod --region us-west-2
  ```

- [ ] **(Optional) Configure Slack/email notifications**
  ```bash
  # Add to terraform.tfvars:
  # slack_webhook_url = "https://hooks.slack.com/services/..."
  # failure_notification_email = "alerts@goodparty.org"
  ```

## Deployment

- [ ] **Initialize Terraform**
  ```bash
  cd infrastructure/environments/prod/serve-analyze-fargate
  AWS_PROFILE=work terraform init
  ```

- [ ] **Run Terraform plan**
  ```bash
  AWS_PROFILE=work terraform plan -out=tfplan
  ```

- [ ] **Review plan output**
  - Expected: 10 resources to add
  - Verify resource names end with `-prod`
  - Check environment variables are correct

- [ ] **Apply Terraform**
  ```bash
  AWS_PROFILE=work terraform apply tfplan
  ```

## Post-Deployment Verification

- [ ] **Check ECS cluster**
  ```bash
  AWS_PROFILE=work aws ecs describe-clusters --clusters serve-analyze-prod --region us-west-2
  ```

- [ ] **Check Lambda function**
  ```bash
  AWS_PROFILE=work aws lambda get-function --function-name serve-analyze-trigger-prod --region us-west-2
  ```

- [ ] **Check Step Functions state machine**
  ```bash
  AWS_PROFILE=work aws stepfunctions describe-state-machine \
    --state-machine-arn arn:aws:states:us-west-2:333022194791:stateMachine:serve-analyze-pipeline-prod \
    --region us-west-2
  ```

- [ ] **Check S3 bucket created**
  ```bash
  AWS_PROFILE=work aws s3 ls | grep serve-analyze-data-prod
  ```

- [ ] **Check SNS topic created**
  ```bash
  AWS_PROFILE=work aws sns list-topics --region us-west-2 | grep serve-analyze-pipeline-failures-prod
  ```

## Testing

- [ ] **Test S3 trigger with sample file**
  ```bash
  # Create test CSV
  echo "phone_number,message_text,direction,timestamp" > test.csv
  echo "+15551234567,Test message,inbound,2024-10-14T10:00:00Z" >> test.csv

  # Upload to S3
  AWS_PROFILE=work aws s3 cp test.csv s3://serve-analyze-data-prod/input/test-$(date +%s).csv
  ```

- [ ] **Monitor Step Functions execution**
  ```bash
  AWS_PROFILE=work aws stepfunctions list-executions \
    --state-machine-arn arn:aws:states:us-west-2:333022194791:stateMachine:serve-analyze-pipeline-prod \
    --region us-west-2
  ```

- [ ] **Check ECS task logs**
  ```bash
  AWS_PROFILE=work aws logs tail /ecs/serve-analyze-prod --follow --region us-west-2
  ```

- [ ] **Verify DynamoDB records created**
  ```bash
  AWS_PROFILE=work aws dynamodb scan \
    --table-name serve-message-v1-prod \
    --select COUNT \
    --region us-west-2
  ```

- [ ] **Test failure notification (optional)**
  ```bash
  # Upload invalid CSV to trigger failure
  echo "invalid" > invalid.csv
  AWS_PROFILE=work aws s3 cp invalid.csv s3://serve-analyze-data-prod/input/invalid-test.csv

  # Check SNS notification received
  ```

## Rollback Plan

If issues occur:

- [ ] **Stop processing new files**
  ```bash
  # Remove S3 notification temporarily
  AWS_PROFILE=work aws s3api put-bucket-notification-configuration \
    --bucket serve-analyze-data-prod \
    --notification-configuration '{}'
  ```

- [ ] **Drain in-flight tasks**
  ```bash
  # Monitor until all executions complete
  AWS_PROFILE=work aws stepfunctions list-executions \
    --state-machine-arn arn:aws:states:us-west-2:333022194791:stateMachine:serve-analyze-pipeline-prod \
    --status-filter RUNNING \
    --region us-west-2
  ```

- [ ] **Rollback infrastructure**
  ```bash
  AWS_PROFILE=work terraform destroy
  ```

## Sign-Off

- [ ] Deployment completed successfully
- [ ] All tests passed
- [ ] Monitoring configured
- [ ] Team notified
- [ ] Documentation updated

**Deployed by**: ___________________
**Date**: ___________________
**Version**: serve-analyze-prod:___________________
