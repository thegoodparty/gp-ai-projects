terraform {
  required_version = ">= 1.5.0"

  backend "s3" {
    bucket       = "goodparty-terraform-state-us-west-2"
    key          = "pmf-engine-control-plane/prod/terraform.tfstate"
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

variable "bootstrap" {
  type        = bool
  default     = false
  description = "First-pass apply: skip the broker cross-stack remote_state lookup so control-plane can be applied before broker. Fargate state must already exist (apply order: fargate -> control-plane(bootstrap=true) -> broker -> control-plane re-apply)."
}

# gp-api's results queue — the same queue the broker sends success results to
# and gp-api's consumer polls. Looked up by name (not an unversioned tfvar) so
# the dispatch Lambda's error callbacks land on the exact queue gp-api reads.
data "aws_sqs_queue" "gp_api_results" {
  name = "master-Queue.fifo"
}

data "terraform_remote_state" "fargate" {
  backend = "s3"
  config = {
    bucket = "goodparty-terraform-state-us-west-2"
    key    = "pmf-engine-fargate/prod/terraform.tfstate"
    region = "us-west-2"
  }
}

data "terraform_remote_state" "broker" {
  count = var.bootstrap ? 0 : 1

  backend = "s3"
  config = {
    bucket = "goodparty-terraform-state-us-west-2"
    key    = "broker/prod/terraform.tfstate"
    region = "us-west-2"
  }
}

data "terraform_remote_state" "agent_experiment_metadata" {
  count = var.bootstrap ? 0 : 1

  backend = "s3"
  config = {
    bucket = "goodparty-terraform-state-us-west-2"
    key    = "agent-experiment-metadata/prod/terraform.tfstate"
    region = "us-west-2"
  }
}

# Direct lookup so control-plane can pass the secret ARN to its dispatch Lambda
# without depending on broker remote_state existing (the secret is created by
# broker/main.tf as a top-level resource, before any cross-stack wiring).
data "aws_secretsmanager_secret" "service_tokens" {
  name = "broker-service-tokens-prod"
}

module "pmf_engine_control_plane" {
  source = "../../../modules/pmf-engine-control-plane"

  environment = "prod"

  ecs_cluster_arn             = data.terraform_remote_state.fargate.outputs.cluster_arn
  ecs_task_definition_family  = data.terraform_remote_state.fargate.outputs.task_definition_family
  ecs_subnet_ids              = ["subnet-053357b931f0524d4", "subnet-0bb591861f72dcb7f"]
  ecs_security_group_id       = data.terraform_remote_state.fargate.outputs.security_group_id
  ecs_task_execution_role_arn = data.terraform_remote_state.fargate.outputs.task_execution_role_arn
  ecs_task_role_arn           = data.terraform_remote_state.fargate.outputs.task_role_arn

  lambda_package_dir = "${path.module}/../../../../pmf_engine/.lambda_build"

  gp_api_sqs_queue_url = data.aws_sqs_queue.gp_api_results.url
  gp_api_sqs_queue_arn = data.aws_sqs_queue.gp_api_results.arn

  broker_url                = var.bootstrap ? "https://broker-bootstrap.placeholder" : try(data.terraform_remote_state.broker[0].outputs.broker_url, "https://broker-bootstrap.placeholder")
  service_tokens_secret_arn = data.aws_secretsmanager_secret.service_tokens.arn

  experiment_metadata_bucket_name     = var.bootstrap ? "" : try(data.terraform_remote_state.agent_experiment_metadata[0].outputs.bucket_name, "")
  experiment_metadata_read_policy_arn = var.bootstrap ? "" : try(data.terraform_remote_state.agent_experiment_metadata[0].outputs.read_policy_arn, "")

  vpc_id                   = "vpc-0763fa52c32ebcf6a"
  broker_security_group_id = var.bootstrap ? "" : try(data.terraform_remote_state.broker[0].outputs.security_group_id, "")

  sns_topic_arn = try(data.terraform_remote_state.fargate.outputs.sns_topic_arn, "")
}

output "dispatch_lambda_sg_id" {
  value       = module.pmf_engine_control_plane.dispatch_lambda_sg_id
  description = "Security group ID of the dispatch Lambda (consumed by broker for ingress rule in Step 4)"
}
