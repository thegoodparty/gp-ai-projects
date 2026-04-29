variable "environment" {
  description = "Environment name (dev, qa, prod)"
  type        = string
}

variable "s3_bucket_name" {
  description = "Existing S3 bucket name for pipeline data (meeting_pipeline/ prefix)"
  type        = string
}

variable "ecr_repository_url" {
  description = "ECR repository URL for Lambda container images"
  type        = string
}

variable "docker_image_tag" {
  description = "Docker image tag for meeting pipeline Lambda functions"
  type        = string
  default     = "meeting-pipeline-dev"
}

variable "vpc_id" {
  description = "VPC ID for Fargate discover task"
  type        = string
}

variable "private_subnet_ids" {
  description = "Private subnet IDs for Fargate tasks"
  type        = list(string)
}

variable "failure_notification_email" {
  description = "Email address to receive failure notifications (optional)"
  type        = string
  default     = ""
}

variable "shared_slack_notifier_lambda_arn" {
  description = "ARN of the shared Slack notifier Lambda function (leave empty to disable Slack notifications)"
  type        = string
  default     = ""
}
