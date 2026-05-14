terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

variable "environment" {
  description = "Environment name (dev, qa, prod). Drives bucket name and IAM role names."
  type        = string

  validation {
    condition     = contains(["dev", "qa", "prod"], var.environment)
    error_message = "environment must be one of: dev, qa, prod"
  }
}

locals {
  bucket_name      = "agent-experiment-metadata-${var.environment}"
  read_policy_name = "AgentExperimentMetadataRead-${var.environment}"
}

# ---------------------------------------------------------------------------
# Metadata bucket
# Holds: index.json, <experiment_id>/manifest.json, <experiment_id>/instruction.md
# ---------------------------------------------------------------------------

resource "aws_s3_bucket" "metadata" {
  bucket = local.bucket_name

  tags = {
    Name        = "agent-experiment-metadata-${var.environment}"
    Environment = var.environment
    ManagedBy   = "terraform"
    Purpose     = "PMF experiment manifests consumed at runtime by Fargate runner + dispatch Lambda + gp-api"
  }
}

resource "aws_s3_bucket_versioning" "metadata" {
  bucket = aws_s3_bucket.metadata.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "metadata" {
  bucket = aws_s3_bucket.metadata.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "metadata" {
  bucket = aws_s3_bucket.metadata.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Lifecycle: keep noncurrent versions for 30 days (rollback window), then expire.
resource "aws_s3_bucket_lifecycle_configuration" "metadata" {
  bucket = aws_s3_bucket.metadata.id

  rule {
    id     = "expire-old-noncurrent-versions"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }
}

# ---------------------------------------------------------------------------
# IAM: managed policy for read access. Other modules (pmf-engine-fargate,
# pmf-engine-control-plane, gp-api task role) attach this via remote_state
# lookup of the read_policy_arn output.
# ---------------------------------------------------------------------------

resource "aws_iam_policy" "metadata_read" {
  name        = local.read_policy_name
  description = "Read access to ${local.bucket_name} (PMF experiment manifests). Attach to runtime task/lambda roles that load manifests at runtime."

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ListBucket"
        Effect   = "Allow"
        Action   = ["s3:ListBucket", "s3:ListBucketVersions"]
        Resource = [aws_s3_bucket.metadata.arn]
      },
      {
        Sid    = "GetObjects"
        Effect = "Allow"
        # GetObjectVersion is needed when callers pass VersionId (deterministic
        # pinning by the dispatch Lambda). GetObject alone covers "latest"
        # reads; both together cover both modes.
        Action   = ["s3:GetObject", "s3:GetObjectVersion"]
        Resource = ["${aws_s3_bucket.metadata.arn}/*"]
      }
    ]
  })
}

# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------

output "bucket_name" {
  description = "Name of the metadata bucket (used as EXPERIMENT_METADATA_BUCKET env var by runner + Lambda)."
  value       = aws_s3_bucket.metadata.bucket
}

output "bucket_arn" {
  description = "ARN of the metadata bucket. Other modules attach IAM policies referencing this."
  value       = aws_s3_bucket.metadata.arn
}

output "read_policy_arn" {
  description = "Managed IAM policy ARN granting read access. Attach to: Fargate task role, dispatch Lambda role, gp-api ECS task role."
  value       = aws_iam_policy.metadata_read.arn
}
