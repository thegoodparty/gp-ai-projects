output "lambda_function_arn" {
  description = "ARN of the Lambda function"
  value       = module.lambda.serve_message_lambda_arn
}

output "lambda_function_name" {
  description = "Name of the Lambda function"
  value       = module.lambda.serve_message_lambda_function_name
}

output "dynamodb_table_name" {
  description = "Name of the DynamoDB table"
  value       = module.dynamodb.table_name
}

output "dynamodb_table_arn" {
  description = "ARN of the DynamoDB table"
  value       = module.dynamodb.table_arn
}