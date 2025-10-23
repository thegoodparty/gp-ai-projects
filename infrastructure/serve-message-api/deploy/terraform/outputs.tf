# DynamoDB Outputs
output "dynamodb_table_name" {
  description = "Name of the DynamoDB table"
  value       = module.dynamodb.table_name
}

output "dynamodb_table_arn" {
  description = "ARN of the DynamoDB table"
  value       = module.dynamodb.table_arn
}

# Lambda Outputs
output "serve_message_lambda_function_name" {
  description = "Name of the unified serve message Lambda function"
  value       = module.lambda.serve_message_lambda_function_name
}

# Function URL Outputs
output "serve_message_function_url" {
  description = "Function URL for the unified serve message Lambda function"
  value       = module.lambda.serve_message_function_url
}

# IAM Outputs
output "set_lambda_credentials_secret_arn" {
  description = "ARN of the Secrets Manager secret containing SET Lambda credentials"
  value       = module.iam.set_lambda_credentials_secret_arn
}

output "set_lambda_credentials_secret_name" {
  description = "Name of the Secrets Manager secret containing SET Lambda credentials"
  value       = module.iam.set_lambda_credentials_secret_name
}

# API Gateway Outputs (DEPRECATED - replaced with Function URLs)
# output "api_gateway_url" {
#   description = "Base URL of the API Gateway"
#   value       = module.api_gateway.api_gateway_url
# }

# output "retrieve_api_key_value" {
#   description = "Value of the RETRIEVE API key"
#   value       = module.api_gateway.retrieve_api_key_value
#   sensitive   = true
# }

# output "api_gateway_domain_name" {
#   description = "API Gateway domain name for ALB origin"
#   value       = module.api_gateway.api_gateway_domain_name
# }

# Environment Configuration for .env file
output "env_configuration" {
  description = "Environment configuration for .env file"
  value = {
    table_name                     = module.dynamodb.table_name
    credentials_secret_name        = module.iam.set_lambda_credentials_secret_name
    serve_message_function_url     = module.lambda.serve_message_function_url
  }
  sensitive = false
}