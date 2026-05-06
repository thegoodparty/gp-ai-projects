terraform {
  required_version = ">= 1.5.0"

  backend "s3" {
    bucket       = "goodparty-terraform-state-us-west-2"
    key          = "agent-experiment-metadata/qa/terraform.tfstate"
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
}

# OIDC provider was created by shared/github-actions-iam. Look it up by URL
# rather than via remote_state so this env doesn't depend on that state file
# being readable from this stack.
data "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"
}

module "agent_experiment_metadata" {
  source = "../../../modules/agent-experiment-metadata"

  environment              = "qa"
  github_oidc_provider_arn = data.aws_iam_openid_connect_provider.github.arn
  publishing_repo          = "thegoodparty/runbooks"
  publish_branch           = "qa"
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

output "publish_role_arn" {
  value = module.agent_experiment_metadata.publish_role_arn
}
