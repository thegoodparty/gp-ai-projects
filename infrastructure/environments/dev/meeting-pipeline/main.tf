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

variable "public_subnet_ids" {
  description = "Public subnet IDs for the discover Fargate task (outbound-only, no NAT)"
  type        = list(string)
}

# ── Remote state references ────────────────────────────────────────────────

data "terraform_remote_state" "shared_ecr" {
  backend = "s3"
  config = {
    bucket = "goodparty-terraform-state-us-west-2"
    key    = "shared/ecr/terraform.tfstate"
    region = "us-west-2"
  }
}

# Optional — set use_slack_notifier = false (or omit shared/slack-notifier
# state entirely) in environments without the shared notifier deployed.
variable "use_slack_notifier" {
  type    = bool
  default = true
}

data "terraform_remote_state" "shared_slack_notifier" {
  count   = var.use_slack_notifier ? 1 : 0
  backend = "s3"
  config = {
    bucket = "goodparty-terraform-state-us-west-2"
    key    = "shared/slack-notifier/terraform.tfstate"
    region = "us-west-2"
  }
}

# NOTE: meeting-qa wiring deferred until the meeting-qa module ships.
# Once meeting-qa/dev/terraform.tfstate exists, add a data
# "terraform_remote_state" "meeting_qa" block here and pass its outputs as
# qa_queue_url / qa_queue_arn below. The module already defaults both to ""
# with documented graceful no-op behavior, so leaving them unset is safe.

# ── Module ─────────────────────────────────────────────────────────────────

module "meeting_pipeline" {
  source = "../../../modules/meeting-pipeline"

  environment        = "dev"
  ecr_repository_url = data.terraform_remote_state.shared_ecr.outputs.repository_url
  docker_image_tag   = "meeting-pipeline-dev"
  vpc_id             = var.vpc_id
  public_subnet_ids  = var.public_subnet_ids

  shared_slack_notifier_lambda_arn = try(data.terraform_remote_state.shared_slack_notifier[0].outputs.lambda_function_arn, "")

  # qa_queue_url / qa_queue_arn intentionally omitted — see note above.
}

# ── Outputs ────────────────────────────────────────────────────────────────

output "s3_bucket_name" {
  value = module.meeting_pipeline.s3_bucket_name
}

output "lambda_function_names" {
  value = {
    scan    = module.meeting_pipeline.scan_lambda_name
    process = module.meeting_pipeline.process_lambda_name
  }
}

output "queue_urls" {
  value = {
    process  = module.meeting_pipeline.process_queue_url
    discover = module.meeting_pipeline.discover_queue_url
  }
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
