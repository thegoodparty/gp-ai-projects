variable "environment" {
  description = "Environment (dev/prod)"
  type        = string
  validation {
    condition     = contains(["dev", "prod"], var.environment)
    error_message = "Environment must be either 'dev' or 'prod'."
  }
}

variable "set_lambda_invoke_arn" {
  description = "Invoke ARN of the SET Lambda function"
  type        = string
}

variable "retrieve_lambda_invoke_arn" {
  description = "Invoke ARN of the RETRIEVE Lambda function"
  type        = string
}

variable "set_lambda_function_name" {
  description = "Name of the SET Lambda function"
  type        = string
}

variable "retrieve_lambda_function_name" {
  description = "Name of the RETRIEVE Lambda function"
  type        = string
}