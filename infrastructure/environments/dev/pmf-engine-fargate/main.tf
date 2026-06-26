terraform {
  required_version = ">= 1.5.0"

  backend "s3" {
    bucket       = "goodparty-terraform-state-us-west-2"
    key          = "pmf-engine-fargate/dev/terraform.tfstate"
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

data "terraform_remote_state" "broker" {
  backend = "s3"
  config = {
    bucket = "goodparty-terraform-state-us-west-2"
    key    = "broker/dev/terraform.tfstate"
    region = "us-west-2"
  }
}

data "terraform_remote_state" "vpc_endpoints" {
  backend = "s3"
  config = {
    bucket = "goodparty-terraform-state-us-west-2"
    key    = "pmf-vpc-endpoints/dev/terraform.tfstate"
    region = "us-west-2"
  }
}

module "pmf_engine_fargate" {
  source = "../../../modules/pmf-engine-fargate"

  environment        = "dev"
  vpc_id             = "vpc-0763fa52c32ebcf6a"
  private_subnet_ids = ["subnet-053357b931f0524d4", "subnet-0bb591861f72dcb7f"]
  ecr_repository_url = "333022194791.dkr.ecr.us-west-2.amazonaws.com/gp-ai-projects"
  docker_image_tag   = "pmf-engine-dev"
  task_cpu           = "1024"
  task_memory        = "2048"

  artifact_bucket_arn      = "arn:aws:s3:::gp-agent-artifacts-dev"
  broker_security_group_id = try(data.terraform_remote_state.broker.outputs.security_group_id, "")
  broker_url               = data.terraform_remote_state.broker.outputs.broker_url
  vpce_security_group_id   = try(data.terraform_remote_state.vpc_endpoints.outputs.vpce_security_group_id, "")

  shared_slack_notifier_lambda_arn = "arn:aws:lambda:us-west-2:333022194791:function:shared-slack-notifier"
}

output "cluster_arn" {
  value = module.pmf_engine_fargate.cluster_arn
}

output "task_definition_family" {
  value = module.pmf_engine_fargate.task_definition_family
}

output "security_group_id" {
  value = module.pmf_engine_fargate.security_group_id
}

output "task_execution_role_arn" {
  value = module.pmf_engine_fargate.task_execution_role_arn
}

output "task_role_arn" {
  value = module.pmf_engine_fargate.task_role_arn
}

output "sns_topic_arn" {
  value = module.pmf_engine_fargate.sns_topic_arn
}
