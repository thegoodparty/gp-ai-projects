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

  default_tags {
    tags = {
      Project = "clickup-bot"
    }
  }
}

# Default is true on purpose. This is the prod environment dir, so the default IS the
# prod value. Previously the default was false and the real value lived only in a
# gitignored local terraform.tfvars; a terraform apply from a checkout without that
# file silently disabled the bot (stripped the Lambda ECS_* env vars and the ECS IAM
# policy) for 12 days starting 2026-06-26. Do not flip this back to false.
variable "enable_fargate_trigger" {
  description = "Whether to enable Fargate task triggering"
  type        = bool
  default     = true
}

# Prod private subnets, pinned here for the same reason (and matching the precedent in
# ../pmf-engine-control-plane/main.tf, which hardcodes these subnets).
variable "private_subnet_ids" {
  description = "Private subnet IDs for ECS tasks"
  type        = list(string)
  default     = ["subnet-053357b931f0524d4", "subnet-0bb591861f72dcb7f"]
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

# Routes the module's handler-error alarm to Slack (same notifier the
# engineer-agent failure topic uses).
data "terraform_remote_state" "shared_slack_notifier" {
  backend = "s3"

  config = {
    bucket = "goodparty-terraform-state-us-west-2"
    key    = "shared/slack-notifier/terraform.tfstate"
    region = "us-west-2"
  }
}

module "clickup_bot" {
  source = "../../../modules/clickup-bot"

  environment = "prod"

  enable_fargate_trigger      = var.enable_fargate_trigger
  ecs_cluster_arn             = var.enable_fargate_trigger ? data.terraform_remote_state.engineer_agent_fargate[0].outputs.cluster_arn : ""
  ecs_task_definition_arn     = var.enable_fargate_trigger ? data.terraform_remote_state.engineer_agent_fargate[0].outputs.task_definition_arn : ""
  ecs_task_definition_family  = var.enable_fargate_trigger ? data.terraform_remote_state.engineer_agent_fargate[0].outputs.task_definition_family : ""
  ecs_subnet_ids              = var.private_subnet_ids
  ecs_security_group_id       = var.enable_fargate_trigger ? data.terraform_remote_state.engineer_agent_fargate[0].outputs.security_group_id : ""
  ecs_task_execution_role_arn = var.enable_fargate_trigger ? data.terraform_remote_state.engineer_agent_fargate[0].outputs.task_execution_role_arn : ""
  ecs_task_role_arn           = var.enable_fargate_trigger ? data.terraform_remote_state.engineer_agent_fargate[0].outputs.task_role_arn : ""

  shared_slack_notifier_lambda_arn = data.terraform_remote_state.shared_slack_notifier.outputs.lambda_function_arn
}

output "failure_sns_topic_arn" {
  value       = module.clickup_bot.failure_sns_topic_arn
  description = "SNS topic that receives clickup-bot handler error alarms"
}

output "lambda_function_arn" {
  value       = module.clickup_bot.lambda_function_arn
  description = "Lambda function ARN"
}

output "lambda_function_name" {
  value       = module.clickup_bot.lambda_function_name
  description = "Lambda function name"
}
