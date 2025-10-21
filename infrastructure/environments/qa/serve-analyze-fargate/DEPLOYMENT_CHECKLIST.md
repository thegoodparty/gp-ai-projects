# QA Environment Deployment Checklist

Follow this checklist to deploy serve-analyze-fargate to the QA environment.

## Pre-Deployment Planning

- [ ] **Decide VPC strategy**
  - [ ] Option 1: Share dev VPC (cost-effective)
  - [ ] Option 2: Use prod VPC (realistic testing)
  - [ ] Option 3: Create dedicated QA VPC

- [ ] **Decide DynamoDB strategy**
  - [ ] Option 1: New table `serve-message-v1-qa` (isolated)
  - [ ] Option 2: Share dev table (cost-effective)

- [ ] **Decide API key strategy**
  - [ ] Option 1: Separate QA API keys (quota isolation)
  - [ ] Option 2: Share dev API keys (simplicity)

## Pre-Deployment Tasks

- [ ] **Build and push Docker image**
  ```bash
  cd /Users/collinpark/work/gp-ai-projects
  docker buildx build --platform linux/arm64 -t serve-analyze-qa -f serve/v1_pipeline/Dockerfile .
  docker tag serve-analyze-qa:latest 333022194791.dkr.ecr.us-west-2.amazonaws.com/gp-ai-projects:serve-analyze-qa
  AWS_PROFILE=work aws ecr get-login-password --region us-west-2 | docker login --username AWS --password-stdin 333022194791.dkr.ecr.us-west-2.amazonaws.com
  docker push 333022194791.dkr.ecr.us-west-2.amazonaws.com/gp-ai-projects:serve-analyze-qa
  ```

- [ ] **Update terraform.tfvars**
  ```bash
  cd infrastructure/environments/qa/serve-analyze-fargate
  vi terraform.tfvars
  # Update VPC, subnets, and API keys
  ```

- [ ] **Verify/create DynamoDB table** (if using separate table)
  ```bash
  AWS_PROFILE=work aws dynamodb describe-table --table-name serve-message-v1-qa --region us-west-2
  ```

- [ ] **(Optional) Configure Slack/email notifications**
  ```bash
  # Add to terraform.tfvars:
  # slack_webhook_url = "https://hooks.slack.com/services/..."
  # failure_notification_email = "qa-alerts@goodparty.org"
  ```

## Deployment

- [ ] **Initialize Terraform**
  ```bash
  cd infrastructure/environments/qa/serve-analyze-fargate
  AWS_PROFILE=work terraform init
  ```

- [ ] **Run Terraform plan**
  ```bash
  AWS_PROFILE=work terraform plan -out=tfplan
  ```

- [ ] **Review plan output**
  - Expected: 10 resources to add
  - Verify resource names end with `-qa`
  - Verify VPC and subnets are correct
  - Check environment variables

- [ ] **Apply Terraform**
  ```bash
  AWS_PROFILE=work terraform apply tfplan
  ```

## Post-Deployment Verification

- [ ] **Check ECS cluster**
  ```bash
  AWS_PROFILE=work aws ecs describe-clusters --clusters serve-analyze-qa --region us-west-2
  ```

- [ ] **Check Lambda function**
  ```bash
  AWS_PROFILE=work aws lambda get-function --function-name serve-analyze-trigger-qa --region us-west-2
  ```

- [ ] **Check Step Functions state machine**
  ```bash
  AWS_PROFILE=work aws stepfunctions describe-state-machine \
    --state-machine-arn arn:aws:states:us-west-2:333022194791:stateMachine:serve-analyze-pipeline-qa \
    --region us-west-2
  ```

- [ ] **Check S3 bucket created**
  ```bash
  AWS_PROFILE=work aws s3 ls | grep serve-analyze-data-qa
  ```

- [ ] **Check SNS topic created**
  ```bash
  AWS_PROFILE=work aws sns list-topics --region us-west-2 | grep serve-analyze-pipeline-failures-qa
  ```

- [ ] **Verify S3 notification configured**
  ```bash
  AWS_PROFILE=work aws s3api get-bucket-notification-configuration \
    --bucket serve-analyze-data-qa --region us-west-2
  ```

## Testing Phase

### 1. Smoke Test

- [ ] **Create minimal test file**
  ```bash
  echo "phone_number,message_text,direction,timestamp" > smoke-test.csv
  echo "+15551234567,Test message,inbound,2024-10-14T10:00:00Z" >> smoke-test.csv
  ```

- [ ] **Upload to S3**
  ```bash
  AWS_PROFILE=work aws s3 cp smoke-test.csv s3://serve-analyze-data-qa/input/smoke-$(date +%s).csv
  ```

- [ ] **Monitor Step Functions execution**
  ```bash
  AWS_PROFILE=work aws stepfunctions list-executions \
    --state-machine-arn arn:aws:states:us-west-2:333022194791:stateMachine:serve-analyze-pipeline-qa \
    --region us-west-2
  ```

- [ ] **Check ECS task logs**
  ```bash
  AWS_PROFILE=work aws logs tail /ecs/serve-analyze-qa --follow --region us-west-2
  ```

- [ ] **Verify DynamoDB record created**
  ```bash
  AWS_PROFILE=work aws dynamodb scan \
    --table-name serve-message-v1-qa \
    --select COUNT \
    --region us-west-2
  ```

- [ ] **Verify S3 output files created**
  ```bash
  AWS_PROFILE=work aws s3 ls s3://serve-analyze-data-qa/output/ --recursive
  ```

### 2. Integration Test

- [ ] **Prepare realistic test dataset** (100-1000 messages)

- [ ] **Upload test dataset**
  ```bash
  AWS_PROFILE=work aws s3 cp integration-test.csv s3://serve-analyze-data-qa/input/
  ```

- [ ] **Monitor full pipeline execution**

- [ ] **Validate classification results**

- [ ] **Validate clustering output**

- [ ] **Validate DynamoDB uploads**

- [ ] **Check execution time and performance**

### 3. Failure Test

- [ ] **Upload invalid CSV to test retry logic**
  ```bash
  echo "invalid" > failure-test.csv
  AWS_PROFILE=work aws s3 cp failure-test.csv s3://serve-analyze-data-qa/input/
  ```

- [ ] **Verify Step Functions retries 3 times**

- [ ] **Verify SNS notification sent after retries exhausted**

- [ ] **Check CloudWatch alarms triggered**

### 4. Load Test (Optional)

- [ ] **Prepare large dataset** (10K+ messages)

- [ ] **Upload large dataset**

- [ ] **Monitor resource utilization**
  - CPU usage
  - Memory usage
  - Task duration

- [ ] **Verify no throttling or timeouts**

- [ ] **Check cost implications**

## Performance Validation

- [ ] **Pipeline completes successfully** ✅ / ❌

- [ ] **Classification accuracy acceptable** ✅ / ❌

- [ ] **Clustering quality acceptable** ✅ / ❌

- [ ] **Execution time within SLA** ✅ / ❌

- [ ] **No data loss or corruption** ✅ / ❌

- [ ] **Error handling works correctly** ✅ / ❌

- [ ] **Retry logic functions as expected** ✅ / ❌

- [ ] **Notifications delivered successfully** ✅ / ❌

## Production Readiness

- [ ] **All smoke tests passed**

- [ ] **All integration tests passed**

- [ ] **Load test completed successfully**

- [ ] **Failure scenarios handled correctly**

- [ ] **Performance meets requirements**

- [ ] **Documentation reviewed and updated**

- [ ] **Team trained on QA environment**

- [ ] **Runbook created for common issues**

## Promotion to Production

- [ ] **QA sign-off received**

- [ ] **Tag Docker image for production**
  ```bash
  docker pull 333022194791.dkr.ecr.us-west-2.amazonaws.com/gp-ai-projects:serve-analyze-qa
  docker tag 333022194791.dkr.ecr.us-west-2.amazonaws.com/gp-ai-projects:serve-analyze-qa \
             333022194791.dkr.ecr.us-west-2.amazonaws.com/gp-ai-projects:serve-analyze-prod
  docker push 333022194791.dkr.ecr.us-west-2.amazonaws.com/gp-ai-projects:serve-analyze-prod
  ```

- [ ] **Deploy to production environment**
  ```bash
  cd ../prod/serve-analyze-fargate
  AWS_PROFILE=work terraform init
  AWS_PROFILE=work terraform plan -out=tfplan
  AWS_PROFILE=work terraform apply tfplan
  ```

- [ ] **Run production smoke tests**

- [ ] **Enable production monitoring**

- [ ] **Update production runbook**

## Rollback Plan

If critical issues found:

- [ ] **Stop new file processing**
  ```bash
  AWS_PROFILE=work aws s3api put-bucket-notification-configuration \
    --bucket serve-analyze-data-qa \
    --notification-configuration '{}'
  ```

- [ ] **Drain in-flight executions**
  ```bash
  # Monitor until all complete
  AWS_PROFILE=work aws stepfunctions list-executions \
    --state-machine-arn arn:aws:states:us-west-2:333022194791:stateMachine:serve-analyze-pipeline-qa \
    --status-filter RUNNING
  ```

- [ ] **Fix issues and redeploy**
  ```bash
  # Make necessary fixes
  AWS_PROFILE=work terraform plan
  AWS_PROFILE=work terraform apply
  ```

- [ ] **Or rollback infrastructure**
  ```bash
  AWS_PROFILE=work terraform destroy
  ```

## Sign-Off

### QA Team
- [ ] Functional testing completed
- [ ] Performance testing completed
- [ ] Security review completed
- [ ] Documentation reviewed

**QA Lead**: ___________________ **Date**: ___________

### DevOps Team
- [ ] Infrastructure deployed correctly
- [ ] Monitoring configured
- [ ] Alerts functioning
- [ ] Backup/recovery tested

**DevOps Lead**: ___________________ **Date**: ___________

### Product Team
- [ ] User acceptance criteria met
- [ ] Edge cases validated
- [ ] Ready for production

**Product Owner**: ___________________ **Date**: ___________

---

**Deployment Version**: serve-analyze-qa:___________________
**Deployment Date**: ___________________
**Next Review Date**: ___________________
