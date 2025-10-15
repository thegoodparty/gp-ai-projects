terraform {
  required_version = ">= 1.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  backend "s3" {
    bucket = "goodparty-terraform-state-us-west-2"
    key    = "serve-analyze-fargate/qa/terraform.tfstate"
    region = "us-west-2"
  }
}

provider "aws" {
  region = "us-west-2"
}

variable "vpc_id" {
  description = "VPC ID for ECS deployment"
  type        = string
}

variable "private_subnet_ids" {
  description = "Private subnet IDs for ECS tasks"
  type        = list(string)
}

variable "failure_notification_email" {
  description = "Email for failure notifications (optional)"
  type        = string
  default     = ""
}

data "terraform_remote_state" "shared_ecr" {
  backend = "s3"

  config = {
    bucket = "goodparty-terraform-state-us-west-2"
    key    = "shared/ecr/terraform.tfstate"
    region = "us-west-2"
  }
}

data "terraform_remote_state" "shared_slack_notifier" {
  backend = "s3"

  config = {
    bucket = "goodparty-terraform-state-us-west-2"
    key    = "shared/slack-notifier/terraform.tfstate"
    region = "us-west-2"
  }
}

module "serve_analyze_fargate" {
  source = "../../../modules/serve-analyze-fargate"

  environment                        = "qa"
  vpc_id                             = var.vpc_id
  private_subnet_ids                 = var.private_subnet_ids
  ecr_repository_url                 = data.terraform_remote_state.shared_ecr.outputs.repository_url
  docker_image_tag                   = "serve-analyze-qa"
  dynamodb_table_name                = "serve-messages-qa"
  shared_slack_notifier_lambda_arn   = data.terraform_remote_state.shared_slack_notifier.outputs.lambda_function_arn
  failure_notification_email         = var.failure_notification_email
}

output "cluster_name" {
  value       = module.serve_analyze_fargate.cluster_name
  description = "ECS cluster name"
}

output "lambda_function_arn" {
  value       = module.serve_analyze_fargate.lambda_function_arn
  description = "Lambda trigger function ARN"
}

output "lambda_function_name" {
  value       = module.serve_analyze_fargate.lambda_function_name
  description = "Lambda trigger function name"
}

output "s3_bucket_name" {
  value       = module.serve_analyze_fargate.s3_bucket_name
  description = "S3 bucket for pipeline data"
}

output "ecr_repository_url" {
  value       = data.terraform_remote_state.shared_ecr.outputs.repository_url
  description = "ECR repository URL for Docker images"
}
