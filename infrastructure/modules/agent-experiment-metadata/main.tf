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

variable "github_oidc_provider_arn" {
  description = "ARN of the existing GitHub Actions OIDC provider (created in shared/github-actions-iam). Pass via remote_state lookup."
  type        = string
}

variable "publishing_repo" {
  description = "GitHub repo (org/name) allowed to assume the publish role. Subject claim restricts to this repo."
  type        = string
  default     = "thegoodparty/runbooks"
}

variable "publish_branch" {
  description = "Git branch in publishing_repo allowed to assume the publish role. Branch-per-env model: dev branch publishes to dev bucket, qa to qa, main to prod."
  type        = string
}

data "aws_caller_identity" "current" {}

locals {
  bucket_name      = "agent-experiment-metadata-${var.environment}"
  publish_role     = "agent-experiment-metadata-publish-${var.environment}"
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
# IAM: GitHub Actions OIDC role for the runbooks repo to publish manifests.
# Trust narrowed to the publishing_repo via the `sub` claim.
# ---------------------------------------------------------------------------

resource "aws_iam_role" "publish" {
  name = local.publish_role

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Federated = var.github_oidc_provider_arn
        }
        Action = "sts:AssumeRoleWithWebIdentity"
        Condition = {
          # Pin trust to ONE branch per env: dev branch can only mint dev
          # bucket creds; qa branch only qa; main branch only prod. AWS
          # rejects an assume-role attempt from any other branch, so a
          # workflow_dispatch from a feature branch or a misconfigured push
          # cannot publish to the wrong env. Anyone with merge-to-<branch>
          # rights can still trigger a publish to that env — that's a
          # separate (social/branch-protection) gate.
          StringEquals = {
            "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
            "token.actions.githubusercontent.com:sub" = "repo:${var.publishing_repo}:ref:refs/heads/${var.publish_branch}"
          }
        }
      }
    ]
  })

  tags = {
    Name        = "agent-experiment-metadata-publish-${var.environment}"
    Environment = var.environment
    Purpose     = "Allow runbooks GitHub Actions to publish PMF experiment manifests"
  }
}

resource "aws_iam_role_policy" "publish_write" {
  name = "publish-write"
  role = aws_iam_role.publish.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ListBucket"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = [aws_s3_bucket.metadata.arn]
      },
      {
        # No DeleteObject by design. The bucket has versioning + 30-day
        # noncurrent lifecycle; bad publishes are rolled back by publishing
        # a new version, never by hard-deleting. Removing an experiment is
        # done by deleting its directory in the runbooks repo and re-running
        # publish (which omits it from index.json) — the orphan files in S3
        # are harmless because the dispatcher routes off index.json. This
        # blocks the `aws s3 rm --recursive` blast radius from a
        # compromised branch.
        Sid    = "ReadWriteObjects"
        Effect = "Allow"
        Action = [
          "s3:PutObject",
          "s3:GetObject"
        ]
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

output "publish_role_arn" {
  description = "IAM role ARN for runbooks GitHub Actions to assume via OIDC for publishing manifests."
  value       = aws_iam_role.publish.arn
}
