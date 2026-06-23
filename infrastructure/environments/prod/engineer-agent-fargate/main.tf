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
    key          = "engineer-agent-fargate/prod/terraform.tfstate"
    region       = "us-west-2"
    use_lockfile = true
    encrypt      = true
  }
}

provider "aws" {
  region = "us-west-2"

  default_tags {
    tags = {
      Project = "engineer-agent"
    }
  }
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

module "engineer_agent_fargate" {
  source = "../../../modules/engineer-agent-fargate"

  environment                      = "prod"
  vpc_id                           = var.vpc_id
  private_subnet_ids               = var.private_subnet_ids
  ecr_repository_url               = data.terraform_remote_state.shared_ecr.outputs.repository_url
  docker_image_tag                 = "engineer-agent-prod"
  shared_slack_notifier_lambda_arn = data.terraform_remote_state.shared_slack_notifier.outputs.lambda_function_arn
  failure_notification_email       = var.failure_notification_email
}

output "cluster_name" {
  value       = module.engineer_agent_fargate.cluster_name
  description = "ECS cluster name"
}

output "cluster_arn" {
  value       = module.engineer_agent_fargate.cluster_arn
  description = "ECS cluster ARN"
}

output "task_definition_arn" {
  value       = module.engineer_agent_fargate.task_definition_arn
  description = "ECS task definition ARN"
}

output "task_definition_family" {
  value       = module.engineer_agent_fargate.task_definition_family
  description = "ECS task definition family"
}

output "security_group_id" {
  value       = module.engineer_agent_fargate.security_group_id
  description = "Security group ID for ECS tasks"
}

output "task_execution_role_arn" {
  value       = module.engineer_agent_fargate.task_execution_role_arn
  description = "Task execution role ARN"
}

output "task_role_arn" {
  value       = module.engineer_agent_fargate.task_role_arn
  description = "Task role ARN"
}

output "ecr_repository_url" {
  value       = data.terraform_remote_state.shared_ecr.outputs.repository_url
  description = "ECR repository URL for Docker images"
}
