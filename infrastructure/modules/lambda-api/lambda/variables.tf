variable "environment" {
  description = "Environment (dev/qa/prod)"
  type        = string
  validation {
    condition     = contains(["dev", "qa", "prod"], var.environment)
    error_message = "Environment must be either 'dev', 'qa', or 'prod'."
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

variable "lambda_source_path" {
  description = "Path to the Lambda source code directory"
  type        = string
}