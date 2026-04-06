variable "environment" {
  description = "Environment name (dev, qa, prod)"
  type        = string
}

variable "output_sqs_queue_arn" {
  description = "ARN of gp-api's existing SQS FIFO queue for completion messages"
  type        = string
}

variable "output_sqs_queue_url" {
  description = "URL of gp-api's existing SQS FIFO queue for completion messages"
  type        = string
}

variable "shared_slack_notifier_lambda_arn" {
  description = "ARN of the shared Slack notifier Lambda function (leave empty to disable Slack notifications)"
  type        = string
  default     = ""
}

variable "failure_notification_email" {
  description = "Email address to receive failure notifications (optional)"
  type        = string
  default     = ""
}
