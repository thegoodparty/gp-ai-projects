terraform {
  required_version = ">= 1.5.0"

  backend "s3" {
    bucket       = "goodparty-terraform-state-us-west-2"
    key          = "broker/prod/terraform.tfstate"
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
  description = "First-pass apply: skip cross-stack remote_state lookups and the artifact bucket data source for stacks/resources that don't exist yet."
}

data "terraform_remote_state" "fargate" {
  count = var.bootstrap ? 0 : 1

  backend = "s3"
  config = {
    bucket = "goodparty-terraform-state-us-west-2"
    key    = "pmf-engine-fargate/prod/terraform.tfstate"
    region = "us-west-2"
  }
}

data "terraform_remote_state" "control_plane" {
  count = var.bootstrap ? 0 : 1

  backend = "s3"
  config = {
    bucket = "goodparty-terraform-state-us-west-2"
    key    = "pmf-engine-control-plane/prod/terraform.tfstate"
    region = "us-west-2"
  }
}

data "aws_s3_bucket" "artifacts" {
  count  = var.bootstrap ? 0 : 1
  bucket = "gp-agent-artifacts-prod"
}

data "aws_sqs_queue" "results" {
  count = var.bootstrap ? 0 : 1
  name  = "master-Queue.fifo"
}

resource "aws_secretsmanager_secret" "service_tokens" {
  name        = "broker-service-tokens-prod"
  description = "Plaintext SERVICE_TOKEN used by the dispatch Lambda to call broker /internal/mint-run-token. Hash lives in broker-prod."

  tags = {
    Environment = "prod"
  }
}

resource "aws_secretsmanager_secret_version" "service_tokens_initial" {
  secret_id     = aws_secretsmanager_secret.service_tokens.id
  secret_string = jsonencode({})

  lifecycle {
    ignore_changes = [secret_string]
  }
}

module "broker" {
  count  = var.bootstrap ? 0 : 1
  source = "../../../modules/broker"

  environment        = "prod"
  vpc_id             = "vpc-0763fa52c32ebcf6a"
  private_subnet_ids = ["subnet-053357b931f0524d4", "subnet-0bb591861f72dcb7f"]
  ecr_repository_url = "333022194791.dkr.ecr.us-west-2.amazonaws.com/gp-ai-projects"
  docker_image_tag   = "broker-prod"

  agent_security_group_id           = try(data.terraform_remote_state.fargate[0].outputs.security_group_id, "")
  dispatch_lambda_security_group_id = try(data.terraform_remote_state.control_plane[0].outputs.dispatch_lambda_sg_id, "")

  artifact_bucket_arn  = data.aws_s3_bucket.artifacts[0].arn
  artifact_bucket_name = data.aws_s3_bucket.artifacts[0].bucket

  results_queue_arn = data.aws_sqs_queue.results[0].arn
  results_queue_url = data.aws_sqs_queue.results[0].url

  gp_api_sqs_queue_arn = data.aws_sqs_queue.results[0].arn

  sns_topic_arn = try(data.terraform_remote_state.fargate[0].outputs.sns_topic_arn, "")
}

output "security_group_id" {
  value       = var.bootstrap ? null : module.broker[0].security_group_id
  description = "Broker security group ID (consumed by fargate + control-plane for egress rules)"
}

output "service_tokens_secret_arn" {
  value       = aws_secretsmanager_secret.service_tokens.arn
  description = "ARN of broker-service-tokens-prod (consumed by control-plane for dispatch Lambda env)"
}

output "broker_url" {
  value       = var.bootstrap ? null : module.broker[0].broker_url
  description = "Service Connect URL for the broker"
}

output "secrets_arn" {
  value       = var.bootstrap ? null : module.broker[0].secrets_arn
  description = "ARN of broker-prod (7 keys including SERVICE_TOKEN_HASH)"
}

output "dynamodb_table_name" {
  value       = var.bootstrap ? null : module.broker[0].dynamodb_table_name
  description = "DynamoDB scope tickets table name"
}
