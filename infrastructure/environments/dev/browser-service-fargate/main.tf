terraform {
  required_version = ">= 1.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  backend "s3" {
    bucket       = "goodparty-terraform-state-us-west-2"
    key          = "browser-service-fargate/dev/terraform.tfstate"
    region       = "us-west-2"
    use_lockfile = true
    encrypt      = true
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

variable "vpc_cidr_block" {
  description = "VPC CIDR block for security group ingress"
  type        = string
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

module "browser_service_fargate" {
  source = "../../../modules/browser-service-fargate"

  environment                = "dev"
  vpc_id                     = var.vpc_id
  private_subnet_ids         = var.private_subnet_ids
  vpc_cidr_block             = var.vpc_cidr_block
  ecr_repository_url         = data.terraform_remote_state.shared_ecr.outputs.repository_url
  docker_image_tag           = "browser-service-dev"
  failure_notification_email = var.failure_notification_email
}

output "cluster_name" {
  value       = module.browser_service_fargate.cluster_name
  description = "ECS cluster name"
}

output "cluster_arn" {
  value       = module.browser_service_fargate.cluster_arn
  description = "ECS cluster ARN"
}

output "task_definition_arn" {
  value       = module.browser_service_fargate.task_definition_arn
  description = "ECS task definition ARN"
}

output "service_name" {
  value       = module.browser_service_fargate.service_name
  description = "ECS service name"
}

output "service_arn" {
  value       = module.browser_service_fargate.service_arn
  description = "ECS service ARN"
}

output "security_group_id" {
  value       = module.browser_service_fargate.security_group_id
  description = "Security group ID for ECS tasks"
}

output "namespace_id" {
  value       = module.browser_service_fargate.namespace_id
  description = "Cloud Map namespace ID"
}

output "discovery_service_arn" {
  value       = module.browser_service_fargate.discovery_service_arn
  description = "Cloud Map service discovery ARN"
}
