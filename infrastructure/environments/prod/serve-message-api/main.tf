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
    key    = "serve-message-api/prod/terraform.tfstate"
    region = "us-west-2"
  }
}

provider "aws" {
  region = var.aws_region
}

module "lambda_api" {
  source = "../../../modules/lambda-api"

  environment        = var.environment
  table_name         = "serve-messages-${var.environment}"
  aws_region         = var.aws_region
  lambda_source_path = abspath("${path.module}/../../../../serve/messages/lambdas/serve-message")
}