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
    key    = "meeting-pipeline/dev/terraform.tfstate"
    region = "us-west-2"

    use_lockfile = true
    encrypt      = true
  }
}

provider "aws" {
  region = "us-west-2"
}

# ── Variables (same pattern as serve-analyze-fargate) ──────────────────────

variable "vpc_id" {
  description = "VPC ID for Fargate discover task"
  type        = string
}

variable "private_subnet_ids" {
  description = "Private subnet IDs for Fargate discover task"
  type        = list(string)
}

# ── Remote state references ────────────────────────────────────────────────

data "terraform_remote_state" "shared_infra" {
  backend = "s3"
  config = {
    bucket = "goodparty-terraform-state-us-west-2"
    key    = "shared-infra/dev/terraform.tfstate"
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

# ── Module ─────────────────────────────────────────────────────────────────

module "meeting_pipeline" {
  source = "../../../modules/meeting-pipeline"

  environment        = "dev"
  s3_bucket_name     = "goodparty-ai-dev"
  ecr_repository_url = data.terraform_remote_state.shared_infra.outputs.ecr_repository_url
  docker_image_tag   = "meeting-pipeline-dev"
  vpc_id             = var.vpc_id
  private_subnet_ids = var.private_subnet_ids

  shared_slack_notifier_lambda_arn = data.terraform_remote_state.shared_slack_notifier.outputs.lambda_function_arn
}

# ── Outputs ────────────────────────────────────────────────────────────────

output "lambda_function_names" {
  value = module.meeting_pipeline.lambda_function_names
}

output "queue_urls" {
  value = module.meeting_pipeline.queue_urls
}

output "step_function_arn" {
  value = module.meeting_pipeline.step_function_arn
}

output "sns_topic_arn" {
  value = module.meeting_pipeline.sns_topic_arn
}

output "ecs_cluster_name" {
  value = module.meeting_pipeline.ecs_cluster_name
}
