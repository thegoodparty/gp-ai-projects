variable "environment" {
  description = "Environment name"
  type        = string
  default     = "prod"
}

variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "us-west-2"
}

variable "custom_domain_name" {
  description = "Custom domain name"
  type        = string
  default     = "ai.goodparty.org"
}

variable "vpc_id" {
  description = "VPC ID"
  type        = string
}

variable "public_subnet_ids" {
  description = "Public subnet IDs"
  type        = list(string)
}

variable "certificate_arn" {
  description = "ACM certificate ARN"
  type        = string
}

variable "serve_message_lambda_arn" {
  description = "Lambda function ARN"
  type        = string
}

variable "serve_message_lambda_function_name" {
  description = "Lambda function name"
  type        = string
}

variable "route53_zone_id" {
  description = "Route53 hosted zone ID"
  type        = string
}