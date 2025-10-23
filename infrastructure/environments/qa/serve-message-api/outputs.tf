output "lambda_function_arn" {
  description = "ARN of the Lambda function"
  value       = module.lambda_api.lambda_function_arn
}

output "lambda_function_name" {
  description = "Name of the Lambda function"
  value       = module.lambda_api.lambda_function_name
}

output "dynamodb_table_name" {
  description = "Name of the DynamoDB table"
  value       = module.lambda_api.dynamodb_table_name
}
