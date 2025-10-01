variable "environment" {
  description = "Environment (dev/prod)"
  type        = string
}

variable "table_name" {
  description = "DynamoDB table name"
  type        = string
}

variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "us-west-2"
}

variable "lambda_source_path" {
  description = "Path to the Lambda source code directory"
  type        = string
}