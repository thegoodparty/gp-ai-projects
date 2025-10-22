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
    key    = "shared-infra/dev/terraform.tfstate"
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

data "terraform_remote_state" "serve_analyze" {
  backend = "s3"

  config = {
    bucket = "goodparty-terraform-state-us-west-2"
    key    = "serve-analyze-fargate/dev/terraform.tfstate"
    region = "us-west-2"
  }
}

module "alb" {
  source = "../../../modules/alb"

  environment       = var.environment
  vpc_id            = var.vpc_id
  public_subnet_ids = var.public_subnet_ids
  certificate_arn   = var.certificate_arn
  api_key           = local.api_key
}

module "route53" {
  source = "../../../modules/route53"

  custom_domain_name = var.custom_domain_name
  route53_zone_id   = var.route53_zone_id
  alb_dns_name      = module.alb.alb_dns_name
  alb_zone_id       = module.alb.alb_zone_id
}

resource "aws_lb_target_group" "serve_analyze" {
  name        = "serve-analyze-${var.environment}"
  target_type = "lambda"

  tags = {
    Name        = "serve-analyze-${var.environment}"
    Environment = var.environment
    Purpose     = "V1 Pipeline Lambda Target Group"
  }
}

resource "aws_lb_target_group_attachment" "serve_analyze" {
  target_group_arn = aws_lb_target_group.serve_analyze.arn
  target_id        = data.terraform_remote_state.serve_analyze.outputs.lambda_function_arn
  depends_on       = [aws_lambda_permission.serve_analyze_alb_invoke]
}

resource "aws_lambda_permission" "serve_analyze_alb_invoke" {
  statement_id  = "AllowExecutionFromALB"
  action        = "lambda:InvokeFunction"
  function_name = data.terraform_remote_state.serve_analyze.outputs.lambda_function_name
  principal     = "elasticloadbalancing.amazonaws.com"
  source_arn    = aws_lb_target_group.serve_analyze.arn
}

resource "aws_lb_listener_rule" "serve_analyze_valid" {
  listener_arn = module.alb.https_listener_arn
  priority     = 10

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.serve_analyze.arn
  }

  condition {
    path_pattern {
      values = ["/serve/messages/process"]
    }
  }

  condition {
    http_header {
      http_header_name = "x-api-key"
      values          = [local.api_key]
    }
  }

  tags = {
    Name        = "serve-analyze-valid-${var.environment}"
    Environment = var.environment
  }
}

resource "aws_lb_listener_rule" "serve_analyze_invalid" {
  listener_arn = module.alb.https_listener_arn
  priority     = 15

  action {
    type = "fixed-response"

    fixed_response {
      content_type = "application/json"
      message_body = jsonencode({
        error = "Forbidden"
        message = "Invalid or missing API key"
      })
      status_code = "403"
    }
  }

  condition {
    path_pattern {
      values = ["/serve/messages/process"]
    }
  }

  tags = {
    Name        = "serve-analyze-invalid-${var.environment}"
    Environment = var.environment
  }
}