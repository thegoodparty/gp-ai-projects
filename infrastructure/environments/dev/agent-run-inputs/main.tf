terraform {
  required_version = ">= 1.5.0"

  backend "s3" {
    bucket       = "goodparty-terraform-state-us-west-2"
    key          = "agent-run-inputs/dev/terraform.tfstate"
    region       = "us-west-2"
    use_lockfile = true
    encrypt      = true
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = "us-west-2"

  default_tags {
    tags = {
      Project = "pmf-engine"
    }
  }
}

module "agent_run_inputs" {
  source = "../../../modules/agent-run-inputs"

  environment = "dev"
}

output "bucket_name" {
  value = module.agent_run_inputs.bucket_name
}

output "bucket_arn" {
  value = module.agent_run_inputs.bucket_arn
}

output "read_policy_arn" {
  value = module.agent_run_inputs.read_policy_arn
}
