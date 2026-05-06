terraform {
  required_version = ">= 1.5.0"

  backend "s3" {
    bucket       = "goodparty-terraform-state-us-west-2"
    key          = "broker/dev/terraform.tfstate"
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

data "terraform_remote_state" "fargate" {
  backend = "s3"
  config = {
    bucket = "goodparty-terraform-state-us-west-2"
    key    = "pmf-engine-fargate/dev/terraform.tfstate"
    region = "us-west-2"
  }
}

data "terraform_remote_state" "control_plane" {
  backend = "s3"
  config = {
    bucket = "goodparty-terraform-state-us-west-2"
    key    = "pmf-engine-control-plane/dev/terraform.tfstate"
    region = "us-west-2"
  }
}

data "aws_s3_bucket" "artifacts" {
  bucket = "gp-agent-artifacts-dev"
}

data "aws_sqs_queue" "results" {
  name = "develop-Queue.fifo"
}

data "terraform_remote_state" "agent_experiment_metadata" {
  backend = "s3"
  config = {
    bucket = "goodparty-terraform-state-us-west-2"
    key    = "agent-experiment-metadata/dev/terraform.tfstate"
    region = "us-west-2"
  }
}

resource "aws_secretsmanager_secret" "service_tokens" {
  name        = "broker-service-tokens-dev"
  description = "Plaintext SERVICE_TOKEN used by the dispatch Lambda to call broker /internal/mint-run-token. Hash lives in broker-dev."

  tags = {
    Environment = "dev"
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
  source = "../../../modules/broker"

  environment        = "dev"
  vpc_id             = "vpc-0763fa52c32ebcf6a"
  private_subnet_ids = ["subnet-053357b931f0524d4", "subnet-0bb591861f72dcb7f"]
  ecr_repository_url = "333022194791.dkr.ecr.us-west-2.amazonaws.com/gp-ai-projects"
  docker_image_tag   = "broker-dev"

  # Cross-SG references: empty on first apply (count-guarded rules skipped),
  # populated on re-apply after fargate + control-plane have been applied.
  agent_security_group_id           = try(data.terraform_remote_state.fargate.outputs.security_group_id, "")
  dispatch_lambda_security_group_id = try(data.terraform_remote_state.control_plane.outputs.dispatch_lambda_sg_id, "")

  artifact_bucket_arn  = data.aws_s3_bucket.artifacts.arn
  artifact_bucket_name = data.aws_s3_bucket.artifacts.bucket

  experiment_metadata_bucket_name     = data.terraform_remote_state.agent_experiment_metadata.outputs.bucket_name
  experiment_metadata_read_policy_arn = data.terraform_remote_state.agent_experiment_metadata.outputs.read_policy_arn

  results_queue_arn = data.aws_sqs_queue.results.arn
  results_queue_url = data.aws_sqs_queue.results.url

  # gp_api_sqs_queue_arn is reference-only in the module; pass a real value for documentation
  gp_api_sqs_queue_arn = data.aws_sqs_queue.results.arn

  sns_topic_arn = try(data.terraform_remote_state.fargate.outputs.sns_topic_arn, "")
}

output "security_group_id" {
  value       = module.broker.security_group_id
  description = "Broker security group ID (consumed by fargate + control-plane for egress rules)"
}

output "service_tokens_secret_arn" {
  value       = aws_secretsmanager_secret.service_tokens.arn
  description = "ARN of broker-service-tokens-dev (consumed by control-plane for dispatch Lambda env)"
}

output "broker_url" {
  value       = module.broker.broker_url
  description = "Service Connect URL for the broker"
}

output "secrets_arn" {
  value       = module.broker.secrets_arn
  description = "ARN of broker-dev (7 keys including SERVICE_TOKEN_HASH)"
}

output "dynamodb_table_name" {
  value       = module.broker.dynamodb_table_name
  description = "DynamoDB scope tickets table name"
}
