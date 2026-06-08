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
  description = "Environment name (dev, qa, prod). Drives bucket name and CORS allowed-origin list."
  type        = string

  validation {
    condition     = contains(["dev", "qa", "prod"], var.environment)
    error_message = "environment must be one of: dev, qa, prod"
  }
}

locals {
  bucket_name      = "gp-agent-run-inputs-${var.environment}"
  read_policy_name = "AgentRunInputsRead-${var.environment}"

  # Browser PUT origins per environment. Env-isolated by design — dev does NOT
  # accept uploads from qa hostnames and vice versa. This diverges from the
  # gp-api Pulumi browser-upload buckets (annotation-attachments, assets) which
  # all share an older cross-env allowlist; align those separately if desired.
  cors_allowed_origins = {
    dev = [
      "http://localhost:4000",
      "https://dev.goodparty.org",
    ]
    qa = [
      "http://localhost:4000",
      "https://gp-ui-git-qa-good-party.vercel.app",
      "https://qa.goodparty.org",
    ]
    prod = [
      "https://goodparty.org",
      "https://www.goodparty.org",
    ]
  }
}

# ---------------------------------------------------------------------------
# Inputs bucket
# Holds user-supplied files consumed by agent experiment runs (first use:
# meeting-briefing agenda PDFs uploaded from /briefings). gp-api issues
# presigned PUTs for browser uploads; the broker reads at runtime via
# /inputs/read (auth gated by ScopeTicket.input_files allowlist).
# ---------------------------------------------------------------------------

resource "aws_s3_bucket" "inputs" {
  bucket        = local.bucket_name
  force_destroy = false

  tags = {
    Name        = local.bucket_name
    Environment = var.environment
    ManagedBy   = "terraform"
    Purpose     = "User-uploaded files consumed by agent runs such as meeting-briefing agenda PDFs"
  }
}

resource "aws_s3_bucket_versioning" "inputs" {
  bucket = aws_s3_bucket.inputs.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "inputs" {
  bucket = aws_s3_bucket.inputs.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "inputs" {
  bucket = aws_s3_bucket.inputs.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Current versions retained indefinitely (audit + re-run support); noncurrent
# versions expire after 30 days to bound storage cost from re-uploads.
resource "aws_s3_bucket_lifecycle_configuration" "inputs" {
  bucket = aws_s3_bucket.inputs.id

  rule {
    id     = "expire-noncurrent-versions"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }
}

# Browser PUTs directly to S3 via the presigned URL gp-api returns. Allowed
# origins are scoped per environment to match the gp-webapp deployment.
resource "aws_s3_bucket_cors_configuration" "inputs" {
  bucket = aws_s3_bucket.inputs.id

  cors_rule {
    allowed_headers = ["*"]
    allowed_methods = ["PUT"]
    allowed_origins = local.cors_allowed_origins[var.environment]
    expose_headers  = ["ETag"]
    max_age_seconds = 3600
  }
}

# ---------------------------------------------------------------------------
# IAM: managed policy for read access. The broker module attaches this via
# remote_state lookup of `read_policy_arn`. Mirrors the agent-experiment-
# metadata pattern so future stacks that need to read this bucket attach the
# same policy rather than each rebuilding it inline.
#
# GetObject only — explicit, no ListBucket. The /inputs/read endpoint always
# fetches an exact key authorized by the ScopeTicket; nothing in the agent
# flow ever enumerates the bucket.
# ---------------------------------------------------------------------------

resource "aws_iam_policy" "inputs_read" {
  name        = local.read_policy_name
  description = "Read access to ${local.bucket_name} (user-uploaded files consumed by agent runs). Attach to runtime task roles that fetch via broker /inputs/read."

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = ["${aws_s3_bucket.inputs.arn}/*"]
      }
    ]
  })
}

# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------

output "bucket_name" {
  description = "Name of the inputs bucket (e.g. gp-agent-run-inputs-dev)."
  value       = aws_s3_bucket.inputs.bucket
}

output "bucket_arn" {
  description = "ARN of the inputs bucket."
  value       = aws_s3_bucket.inputs.arn
}

output "read_policy_arn" {
  description = "Managed IAM policy ARN granting GetObject on the inputs bucket. Attach to: broker task role (for /inputs/read). Add other consumers here as they appear."
  value       = aws_iam_policy.inputs_read.arn
}
