# Lambda API Module - DynamoDB + Lambda + IAM

module "dynamodb" {
  source = "./dynamodb"

  table_name  = var.table_name
  environment = var.environment
}

module "iam" {
  source = "./iam"

  environment        = var.environment
  dynamodb_table_arn = module.dynamodb.table_arn
}

module "lambda" {
  source = "./lambda"

  environment               = var.environment
  set_lambda_role_arn      = module.iam.set_lambda_role_arn
  retrieve_lambda_role_arn = module.iam.retrieve_lambda_role_arn
  dynamodb_table_name      = module.dynamodb.table_name
  lambda_source_path       = var.lambda_source_path
}