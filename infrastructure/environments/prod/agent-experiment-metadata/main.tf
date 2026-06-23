terraform {
  required_version = ">= 1.5.0"

  backend "s3" {
    bucket       = "goodparty-terraform-state-us-west-2"
    key          = "agent-experiment-metadata/prod/terraform.tfstate"
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

module "agent_experiment_metadata" {
  source = "../../../modules/agent-experiment-metadata"

  environment = "prod"
}

output "bucket_name" {
  value = module.agent_experiment_metadata.bucket_name
}

output "bucket_arn" {
  value = module.agent_experiment_metadata.bucket_arn
}

output "read_policy_arn" {
  value = module.agent_experiment_metadata.read_policy_arn
}
