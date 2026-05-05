variable "environment" {
  description = "Environment name (dev, qa, prod)"
  type        = string
}

variable "s3_bucket_name" {
  description = "S3 bucket name (e.g. goodparty-ai-dev). Lambda reads briefings from and writes QA outputs to meeting_pipeline/* prefix."
  type        = string
}

variable "ecr_repository_url" {
  description = "ECR repository URL where the QA Lambda image is pushed. Image tag is meeting-qa-<environment>."
  type        = string
}

variable "docker_image_tag" {
  description = "Docker image tag for the QA Lambda image. Defaults to meeting-qa-<environment>."
  type        = string
  default     = ""
}

variable "lambda_timeout_seconds" {
  description = "Max execution time per QA invocation. QA does up to ~30-60s of LLM calls per briefing; 300 leaves room."
  type        = number
  default     = 300
}

variable "lambda_memory_mb" {
  description = "Lambda memory. PDF text extraction (pymupdf) benefits from more memory."
  type        = number
  default     = 1024
}

variable "lambda_reserved_concurrency" {
  description = "Cap on concurrent QA Lambda invocations. Prevents SQS-driven fan-out from blowing Anthropic / Gemini per-minute quotas."
  type        = number
  default     = 5
}

variable "log_retention_days" {
  description = "CloudWatch log retention for the QA Lambda."
  type        = number
  default     = 90
}
