output "serve_message_lambda_function_name" {
  description = "Name of the unified serve message Lambda function"
  value       = aws_lambda_function.serve_message.function_name
}

output "serve_message_lambda_arn" {
  description = "ARN of the unified serve message Lambda function"
  value       = aws_lambda_function.serve_message.arn
}

output "serve_message_lambda_invoke_arn" {
  description = "Invoke ARN of the unified serve message Lambda function"
  value       = aws_lambda_function.serve_message.invoke_arn
}

output "serve_message_function_url" {
  description = "Function URL for the unified serve message Lambda function"
  value       = aws_lambda_function_url.serve_message_function_url.function_url
}