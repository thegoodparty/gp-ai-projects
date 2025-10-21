variable "environment" {
  description = "Environment (dev/qa/prod)"
  type        = string
  validation {
    condition     = contains(["dev", "qa", "prod"], var.environment)
    error_message = "Environment must be 'dev', 'qa', or 'prod'."
  }
}

variable "custom_domain_name" {
  description = "Custom domain name for ALB (e.g., ai-dev.goodparty.org)"
  type        = string
}

variable "vpc_id" {
  description = "VPC ID where ALB will be deployed"
  type        = string
}

variable "public_subnet_ids" {
  description = "List of public subnet IDs for ALB deployment"
  type        = list(string)
  validation {
    condition     = length(var.public_subnet_ids) >= 2
    error_message = "At least 2 public subnets are required for ALB deployment."
  }
}

variable "serve_message_lambda_arn" {
  description = "ARN of the Lambda function to target"
  type        = string
}

variable "serve_message_lambda_function_name" {
  description = "Name of the Lambda function for permissions"
  type        = string
}

variable "api_key" {
  description = "API key for authentication (set via TF_VAR_api_key environment variable)"
  type        = string
  sensitive   = true
}

variable "certificate_arn" {
  description = "ACM certificate ARN for the custom domain"
  type        = string
}

variable "route53_zone_id" {
  description = "Route53 hosted zone ID for goodparty.org"
  type        = string
}

