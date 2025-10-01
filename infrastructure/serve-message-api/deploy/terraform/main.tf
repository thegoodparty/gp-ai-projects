terraform {
  required_version = ">= 1.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
  }
}

# DynamoDB Module
module "dynamodb" {
  source = "./modules/dynamodb"

  table_name  = "serve-messages-${var.environment}"
  environment = var.environment
}

# IAM Module
module "iam" {
  source = "./modules/iam"

  environment        = var.environment
  dynamodb_table_arn = module.dynamodb.table_arn
}

# Lambda Module
module "lambda" {
  source = "./modules/lambda"

  environment               = var.environment
  set_lambda_role_arn      = module.iam.set_lambda_role_arn
  retrieve_lambda_role_arn = module.iam.retrieve_lambda_role_arn
  dynamodb_table_name      = module.dynamodb.table_name
}

# API Gateway Module (DEPRECATED - removed in favor of ALB + Lambda Function URLs)
# module "api_gateway" {
#   source = "./modules/api_gateway"
#
#   environment                   = var.environment
#   set_lambda_invoke_arn        = module.lambda.set_lambda_invoke_arn
#   retrieve_lambda_invoke_arn   = module.lambda.retrieve_lambda_invoke_arn
#   set_lambda_function_name     = module.lambda.set_lambda_function_name
#   retrieve_lambda_function_name = module.lambda.retrieve_lambda_function_name
# }

# Note: ALB and Route53 are deployed separately via infrastructure/shared/
# This keeps service deployments fast and independent