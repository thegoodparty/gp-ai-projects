terraform {
  required_version = ">= 1.5.0"

  backend "s3" {
    bucket       = "goodparty-terraform-state-us-west-2"
    key          = "pmf-engine-control-plane/qa/terraform.tfstate"
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

variable "bootstrap" {
  type        = bool
  default     = false
  description = "First-pass apply: skip the broker cross-stack remote_state lookup so control-plane can be applied before broker. Fargate state must already exist (apply order: fargate -> control-plane(bootstrap=true) -> broker -> control-plane re-apply)."
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
    key    = "pmf-engine-fargate/qa/terraform.tfstate"
    region = "us-west-2"
  }
}

data "terraform_remote_state" "broker" {
  count = var.bootstrap ? 0 : 1

  backend = "s3"
  config = {
    bucket = "goodparty-terraform-state-us-west-2"
    key    = "broker/qa/terraform.tfstate"
    region = "us-west-2"
  }
}

# Direct lookup so control-plane can pass the secret ARN to its dispatch Lambda
# without depending on broker remote_state existing (the secret is created by
# broker/main.tf as a top-level resource, before any cross-stack wiring).
data "aws_secretsmanager_secret" "service_tokens" {
  name = "broker-service-tokens-qa"
}

module "pmf_engine_control_plane" {
  source = "../../../modules/pmf-engine-control-plane"

  environment = "qa"

  ecs_cluster_arn             = data.terraform_remote_state.fargate.outputs.cluster_arn
  ecs_task_definition_family  = data.terraform_remote_state.fargate.outputs.task_definition_family
  ecs_subnet_ids              = ["subnet-053357b931f0524d4", "subnet-0bb591861f72dcb7f"]
  ecs_security_group_id       = data.terraform_remote_state.fargate.outputs.security_group_id
  ecs_task_execution_role_arn = data.terraform_remote_state.fargate.outputs.task_execution_role_arn
  ecs_task_role_arn           = data.terraform_remote_state.fargate.outputs.task_role_arn

  lambda_package_dir = "${path.module}/../../../../pmf_engine/.lambda_build"

  gp_api_sqs_queue_url = var.gp_api_sqs_queue_url
  gp_api_sqs_queue_arn = var.gp_api_sqs_queue_arn

  broker_url                = var.bootstrap ? "https://broker-bootstrap.placeholder" : try(data.terraform_remote_state.broker[0].outputs.broker_url, "https://broker-bootstrap.placeholder")
  service_tokens_secret_arn = data.aws_secretsmanager_secret.service_tokens.arn

  vpc_id                   = "vpc-0763fa52c32ebcf6a"
  broker_security_group_id = var.bootstrap ? "" : try(data.terraform_remote_state.broker[0].outputs.security_group_id, "")

  sns_topic_arn = try(data.terraform_remote_state.fargate.outputs.sns_topic_arn, "")
}

output "dispatch_lambda_sg_id" {
  value       = module.pmf_engine_control_plane.dispatch_lambda_sg_id
  description = "Security group ID of the dispatch Lambda (consumed by broker for ingress rule in Step 4)"
}
