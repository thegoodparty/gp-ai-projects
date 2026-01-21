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
    key          = "clickup-bot/prod/terraform.tfstate"
    region       = "us-west-2"
    use_lockfile = true
    encrypt      = true
  }
}

provider "aws" {
  region = "us-west-2"
}

variable "enable_fargate_trigger" {
  description = "Whether to enable Fargate task triggering"
  type        = bool
  default     = false
}

variable "private_subnet_ids" {
  description = "Private subnet IDs for ECS tasks"
  type        = list(string)
  default     = []
}

data "terraform_remote_state" "engineer_agent_fargate" {
  count   = var.enable_fargate_trigger ? 1 : 0
  backend = "s3"

  config = {
    bucket = "goodparty-terraform-state-us-west-2"
    key    = "engineer-agent-fargate/prod/terraform.tfstate"
    region = "us-west-2"
  }
}

module "clickup_bot" {
  source = "../../../modules/clickup-bot"

  environment = "prod"

  enable_fargate_trigger       = var.enable_fargate_trigger
  ecs_cluster_arn              = var.enable_fargate_trigger ? data.terraform_remote_state.engineer_agent_fargate[0].outputs.cluster_arn : ""
  ecs_task_definition_arn      = var.enable_fargate_trigger ? data.terraform_remote_state.engineer_agent_fargate[0].outputs.task_definition_arn : ""
  ecs_task_definition_family   = var.enable_fargate_trigger ? data.terraform_remote_state.engineer_agent_fargate[0].outputs.task_definition_family : ""
  ecs_subnet_ids               = var.private_subnet_ids
  ecs_security_group_id        = var.enable_fargate_trigger ? data.terraform_remote_state.engineer_agent_fargate[0].outputs.security_group_id : ""
  ecs_task_execution_role_arn  = var.enable_fargate_trigger ? data.terraform_remote_state.engineer_agent_fargate[0].outputs.task_execution_role_arn : ""
  ecs_task_role_arn            = var.enable_fargate_trigger ? data.terraform_remote_state.engineer_agent_fargate[0].outputs.task_role_arn : ""
}

output "lambda_function_arn" {
  value       = module.clickup_bot.lambda_function_arn
  description = "Lambda function ARN"
}

output "lambda_function_name" {
  value       = module.clickup_bot.lambda_function_name
  description = "Lambda function name"
}
