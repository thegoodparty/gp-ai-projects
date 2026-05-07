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
    key    = "meeting-qa/dev/terraform.tfstate"
    region = "us-west-2"

    use_lockfile = true
    encrypt      = true
  }
}

provider "aws" {
  region = "us-west-2"
}

# ── Remote state references ────────────────────────────────────────────────

# shared/ecr is the actual ECR repo state. shared-infra/dev is the ALB stack
# and does not export an ECR URL.
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

# ── Module ─────────────────────────────────────────────────────────────────

module "meeting_qa" {
  source = "../../../modules/meeting-qa"

  environment        = "dev"
  s3_bucket_name     = "meeting-pipeline-dev"
  ecr_repository_url = data.terraform_remote_state.shared_ecr.outputs.repository_url

  shared_slack_notifier_lambda_arn = try(data.terraform_remote_state.shared_slack_notifier[0].outputs.lambda_function_arn, "")
}

# ── Outputs ────────────────────────────────────────────────────────────────

output "lambda_function_name" {
  value = module.meeting_qa.lambda_function_name
}

output "queue_url" {
  value = module.meeting_qa.queue_url
}

output "queue_arn" {
  value = module.meeting_qa.queue_arn
}

output "sns_topic_arn" {
  value = module.meeting_qa.sns_topic_arn
}
