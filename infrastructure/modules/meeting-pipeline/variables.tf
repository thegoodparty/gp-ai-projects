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

variable "public_subnet_ids" {
  description = "Public subnet IDs for the discover Fargate task. The task is outbound-only (no ingress in its security group) and runs in public subnets with auto-assigned public IPs to avoid the cost of a NAT gateway."
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

variable "qa_queue_url" {
  description = "URL of the meeting-qa SQS queue (sourced from terraform_remote_state of the meeting-qa module). The process Lambda sends to this queue after briefing generation. Empty string disables QA dispatch (graceful no-op)."
  type        = string
  default     = ""
}

variable "qa_queue_arn" {
  description = "ARN of the meeting-qa SQS queue (sourced from terraform_remote_state of the meeting-qa module). Used to grant the process Lambda's role sqs:SendMessage on it."
  type        = string
  default     = ""
}
