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

data "terraform_remote_state" "shared_infra" {
  backend = "s3"
  config = {
    bucket = "goodparty-terraform-state-us-west-2"
    key    = "shared-infra/dev/terraform.tfstate"
    region = "us-west-2"
  }
}

# ── Module ─────────────────────────────────────────────────────────────────

module "meeting_qa" {
  source = "../../../modules/meeting-qa"

  environment        = "dev"
  s3_bucket_name     = "goodparty-ai-dev"
  ecr_repository_url = data.terraform_remote_state.shared_infra.outputs.ecr_repository_url
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
