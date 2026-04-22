terraform {
  required_version = ">= 1.5.0"

  backend "s3" {
    bucket       = "goodparty-terraform-state-us-west-2"
    key          = "pmf-engine-control-plane/dev/terraform.tfstate"
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

variable "gp_api_sqs_queue_url" {
  type        = string
  description = "URL of the gp-api SQS results queue that receives forwarded callbacks. Set per environment via terraform.tfvars; no default so dev values do not leak into qa/prod."
}

variable "gp_api_sqs_queue_arn" {
  type        = string
  description = "ARN of the gp-api SQS results queue. Set per environment via terraform.tfvars."
}

data "terraform_remote_state" "fargate" {
  backend = "s3"
  config = {
    bucket = "goodparty-terraform-state-us-west-2"
    key    = "pmf-engine-fargate/dev/terraform.tfstate"
    region = "us-west-2"
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

module "pmf_engine_control_plane" {
  source = "../../../modules/pmf-engine-control-plane"

  environment = "dev"

  ecs_cluster_arn            = data.terraform_remote_state.fargate.outputs.cluster_arn
  ecs_task_definition_family = data.terraform_remote_state.fargate.outputs.task_definition_family
  ecs_subnet_ids             = ["subnet-053357b931f0524d4", "subnet-0bb591861f72dcb7f"]
  ecs_security_group_id      = data.terraform_remote_state.fargate.outputs.security_group_id
  ecs_task_execution_role_arn = data.terraform_remote_state.fargate.outputs.task_execution_role_arn
  ecs_task_role_arn          = data.terraform_remote_state.fargate.outputs.task_role_arn

  lambda_package_dir = "${path.module}/../../../../pmf_engine/.lambda_build"

  gp_api_sqs_queue_url = var.gp_api_sqs_queue_url
  gp_api_sqs_queue_arn = var.gp_api_sqs_queue_arn

  broker_url                = data.terraform_remote_state.broker.outputs.broker_url
  service_tokens_secret_arn = data.terraform_remote_state.broker.outputs.service_tokens_secret_arn

  vpc_id                   = "vpc-0763fa52c32ebcf6a"
  broker_security_group_id = try(data.terraform_remote_state.broker.outputs.security_group_id, "")

  sns_topic_arn = try(data.terraform_remote_state.fargate.outputs.sns_topic_arn, "")
}

output "dispatch_lambda_sg_id" {
  value       = module.pmf_engine_control_plane.dispatch_lambda_sg_id
  description = "Security group ID of the dispatch Lambda (consumed by broker for ingress rule in Step 4)"
}
