variable "environment" {
  description = "Environment (dev/prod)"
  type        = string
  validation {
    condition     = contains(["dev", "prod"], var.environment)
    error_message = "Environment must be either 'dev' or 'prod'."
  }
}

variable "dynamodb_table_arn" {
  description = "ARN of the DynamoDB table"
  type        = string
}