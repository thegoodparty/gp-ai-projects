variable "environment" {
  description = "Environment (dev/prod)"
  type        = string
  validation {
    condition     = contains(["dev", "prod"], var.environment)
    error_message = "Environment must be either 'dev' or 'prod'."
  }
}

variable "set_lambda_role_arn" {
  description = "ARN of the IAM role for SET Lambda"
  type        = string
}

variable "retrieve_lambda_role_arn" {
  description = "ARN of the IAM role for RETRIEVE Lambda"
  type        = string
}

variable "dynamodb_table_name" {
  description = "Name of the DynamoDB table"
  type        = string
}