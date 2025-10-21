# Security Improvements for Serve-Analyze Fargate Infrastructure

## Summary

This document outlines the security improvements made to the `serve-analyze-fargate` Terraform module to address identified vulnerabilities and follow AWS best practices.

## Issues Fixed

### 1. ✅ S3 Bucket Missing Encryption and Public Access Block

**Issue:** The `pipeline_data` S3 bucket lacked server-side encryption and public access blocking, exposing data to potential unauthorized access.

**Fix:** Added comprehensive S3 security configurations.

**Changes Made** (`main.tf:53-71`):

```hcl
resource "aws_s3_bucket_server_side_encryption_configuration" "pipeline_data" {
  bucket = aws_s3_bucket.pipeline_data.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "pipeline_data" {
  bucket = aws_s3_bucket.pipeline_data.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
```

**Benefits:**
- ✅ **Encryption at rest** - All objects encrypted with AES256
- ✅ **S3 Bucket Keys** enabled for cost savings on KMS requests
- ✅ **Complete public access blocking** - Prevents accidental public exposure
- ✅ **Compliance** - Meets security standards requiring encryption

---

### 2. ✅ Overprivileged Step Functions IAM Policy

**Issue:** Step Functions role had `Resource = "*"` for ECS actions, violating the principle of least privilege.

**Original Policy:**
```hcl
Action = [
  "ecs:RunTask",
  "ecs:StopTask",
  "ecs:DescribeTasks",
  "ecs:TagResource"
]
Resource = "*"  # TOO BROAD!
```

**Fix:** Scoped permissions to specific ECS cluster and task definitions.

**Changes Made** (`main.tf:530-571`):

```hcl
resource "aws_iam_role_policy" "step_functions_ecs" {
  name = "ecs-execution"
  role = aws_iam_role.step_functions.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "ecs:RunTask"
        ]
        Resource = [
          aws_ecs_task_definition.pipeline.arn,
          "${aws_ecs_task_definition.pipeline.arn}:*"
        ]
        Condition = {
          ArnEquals = {
            "ecs:cluster" = aws_ecs_cluster.pipeline.arn
          }
        }
      },
      {
        Effect = "Allow"
        Action = [
          "ecs:StopTask",
          "ecs:DescribeTasks"
        ]
        Resource = "arn:aws:ecs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:task/${aws_ecs_cluster.pipeline.name}/*"
        Condition = {
          ArnEquals = {
            "ecs:cluster" = aws_ecs_cluster.pipeline.arn
          }
        }
      },
      {
        Effect = "Allow"
        Action = [
          "ecs:TagResource"
        ]
        Resource = "arn:aws:ecs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:task/${aws_ecs_cluster.pipeline.name}/*"
      }
    ]
  })
}
```

**Benefits:**
- ✅ **Least Privilege** - Only access to specific cluster and task definitions
- ✅ **Condition-based restrictions** - Additional cluster ARN checks
- ✅ **Separation of permissions** - Different actions scoped appropriately
- ✅ **Reduced blast radius** - Cannot affect other ECS resources

**Permission Scoping Details:**

| Action | Scope | Reason |
|--------|-------|--------|
| `ecs:RunTask` | Specific task definition ARN | Can only launch this pipeline's tasks |
| `ecs:StopTask` | Tasks in this cluster only | Can only stop tasks in serve-analyze cluster |
| `ecs:DescribeTasks` | Tasks in this cluster only | Read-only access limited to cluster |
| `ecs:TagResource` | Tasks in this cluster only | Can only tag pipeline tasks |

---

### 3. ✅ Missing Lambda Deployment Package

**Issue:** Lambda function referenced `lambda-trigger.zip` without proper path or build documentation.

**Original Configuration:**
```hcl
resource "aws_lambda_function" "pipeline_trigger" {
  filename      = "lambda-trigger.zip"  # Ambiguous path!
  # Missing source_code_hash
}
```

**Fix:**
1. Updated Terraform to use proper module path
2. Added source code hash for change detection
3. Created build script and documentation

**Changes Made** (`main.tf:341-343`):

```hcl
resource "aws_lambda_function" "pipeline_trigger" {
  filename         = "${path.module}/lambda-trigger/lambda-trigger.zip"
  source_code_hash = filebase64sha256("${path.module}/lambda-trigger/lambda-trigger.zip")
  function_name    = "serve-analyze-trigger-${var.environment}"
  # ...
}
```

**Additional Files Created:**

1. **`lambda-trigger/build.sh`** - Automated build script
   ```bash
   #!/bin/bash
   npm ci
   npm run build
   # Creates lambda-trigger.zip with compiled code
   ```

2. **`lambda-trigger/README.md`** - Complete build and deployment documentation

**Benefits:**
- ✅ **Proper path resolution** - Uses `${path.module}` for reliable file location
- ✅ **Automatic updates** - `source_code_hash` triggers Lambda updates on code changes
- ✅ **Reproducible builds** - Documented build process
- ✅ **Developer-friendly** - Clear instructions for building and deploying

---

### 4. ✅ ECS Tasks Assigned Public IPs (Previously Fixed)

**Issue:** ECS tasks in private subnets were assigned public IPs unnecessarily.

**Fix:** Changed `AssignPublicIp: "ENABLED"` to `"DISABLED"` in Step Function definition.

**File:** `step-function-definition.json:17`

**Benefits:**
- ✅ **Reduced attack surface** - No direct public IP exposure
- ✅ **NAT Gateway routing** - All outbound through NAT Gateway
- ✅ **Network security** - Tasks remain in private subnet

---

## Deployment Steps

### Prerequisites

Before deploying these changes:

1. **Build Lambda package:**
   ```bash
   cd infrastructure/modules/serve-analyze-fargate/lambda-trigger
   ./build.sh
   ```

2. **Verify Lambda zip exists:**
   ```bash
   ls -lh infrastructure/modules/serve-analyze-fargate/lambda-trigger/lambda-trigger.zip
   ```

### Apply Changes

1. **Navigate to environment directory:**
   ```bash
   cd infrastructure/environments/dev/serve-analyze-fargate
   ```

2. **Plan the changes:**
   ```bash
   terraform plan
   ```

3. **Review the plan** - You should see:
   - `aws_s3_bucket.pipeline_data` will be **updated in-place** (no recreation)
   - `aws_s3_bucket_server_side_encryption_configuration.pipeline_data` will be **created**
   - `aws_s3_bucket_public_access_block.pipeline_data` will be **created**
   - `aws_iam_role_policy.step_functions_ecs` will be **updated in-place**
   - `aws_lambda_function.pipeline_trigger` will be **updated in-place**

4. **Apply the changes:**
   ```bash
   terraform apply
   ```

### Verification

After deployment, verify the changes:

1. **S3 Encryption:**
   ```bash
   aws s3api get-bucket-encryption --bucket serve-analyze-data-dev
   ```

2. **S3 Public Access Block:**
   ```bash
   aws s3api get-public-access-block --bucket serve-analyze-data-dev
   ```

3. **IAM Policy Scope:**
   ```bash
   aws iam get-role-policy \
     --role-name serve-analyze-step-functions-dev \
     --policy-name ecs-execution
   ```

4. **Lambda Source Code Hash:**
   ```bash
   aws lambda get-function --function-name serve-analyze-trigger-dev \
     --query 'Configuration.CodeSha256'
   ```

---

## Security Best Practices Implemented

### S3 Security
- ✅ Server-side encryption enabled (AES256)
- ✅ S3 Bucket Keys enabled for cost optimization
- ✅ Complete public access blocking
- ✅ Lifecycle policies for data retention

### IAM Security
- ✅ Least privilege access (scoped resources)
- ✅ Condition-based restrictions
- ✅ Explicit resource ARNs
- ✅ No wildcard (*) permissions

### Lambda Security
- ✅ Source code integrity checking
- ✅ Proper file path resolution
- ✅ Documented build process
- ✅ Version control friendly

### Network Security
- ✅ Private subnet deployment
- ✅ No public IP assignment
- ✅ NAT Gateway for outbound traffic
- ✅ Security group egress only

---

## Compliance Alignment

These changes align with:

- **CIS AWS Foundations Benchmark**
  - 2.1.1: Ensure S3 bucket encryption is enabled
  - 2.1.5: Ensure S3 buckets block public access

- **AWS Well-Architected Framework**
  - Security Pillar: Least privilege access
  - Security Pillar: Data protection at rest

- **NIST Cybersecurity Framework**
  - PR.AC-4: Access permissions managed
  - PR.DS-1: Data-at-rest protected

---

## Rollback Plan

If issues arise, you can rollback by:

1. **Revert Git changes:**
   ```bash
   git checkout HEAD~1 infrastructure/modules/serve-analyze-fargate/main.tf
   ```

2. **Apply previous configuration:**
   ```bash
   terraform apply
   ```

**Note:** S3 encryption and public access blocks are non-destructive changes and can remain even during rollback.

---

## Future Improvements

Consider implementing:

1. **KMS Encryption** - Use customer-managed KMS keys instead of AES256
2. **VPC Endpoints** - Add VPC endpoints for S3 and DynamoDB to avoid NAT Gateway costs
3. **Lambda Layers** - Extract common dependencies to Lambda layers
4. **Automated Rotation** - Implement IAM access key rotation for any remaining static credentials
5. **CloudTrail Logging** - Enable S3 data events for audit trail

---

## References

- [AWS S3 Security Best Practices](https://docs.aws.amazon.com/AmazonS3/latest/userguide/security-best-practices.html)
- [AWS IAM Best Practices](https://docs.aws.amazon.com/IAM/latest/UserGuide/best-practices.html)
- [Terraform AWS Provider - S3 Bucket](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/s3_bucket)
- [Step Functions IAM Policies](https://docs.aws.amazon.com/step-functions/latest/dg/procedure-create-iam-role.html)
