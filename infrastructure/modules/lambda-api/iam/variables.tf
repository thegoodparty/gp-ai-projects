variable "environment" {
  description = "Environment (dev/qa/prod)"
  type        = string
  validation {
    condition     = contains(["dev", "qa", "prod"], var.environment)
    error_message = "Environment must be either 'dev', 'qa', or 'prod'."
  }
}

variable "dynamodb_table_arn" {
  description = "ARN of the DynamoDB table"
  type        = string
}