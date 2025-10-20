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
    key    = "shared-infra/prod/terraform.tfstate"
    region = "us-west-2"

    use_lockfile = true
    encrypt      = true
  }
}

provider "aws" {
  region = var.aws_region
}

data "aws_secretsmanager_secret_version" "ai_secrets" {
  secret_id = "AI_SECRETS_${upper(var.environment)}"
}

locals {
  ai_secrets = jsondecode(data.aws_secretsmanager_secret_version.ai_secrets.secret_string)
  api_key    = local.ai_secrets["SERVE_API_KEY"]
}

module "alb" {
  source = "../../../modules/alb"

  environment                        = var.environment
  vpc_id                            = var.vpc_id
  public_subnet_ids                 = var.public_subnet_ids
  certificate_arn                   = var.certificate_arn
  serve_message_lambda_arn          = var.serve_message_lambda_arn
  serve_message_lambda_function_name = var.serve_message_lambda_function_name
  api_key                           = local.api_key
}

module "route53" {
  source = "../../../modules/route53"

  custom_domain_name = var.custom_domain_name
  route53_zone_id   = var.route53_zone_id
  alb_dns_name      = module.alb.alb_dns_name
  alb_zone_id       = module.alb.alb_zone_id
}