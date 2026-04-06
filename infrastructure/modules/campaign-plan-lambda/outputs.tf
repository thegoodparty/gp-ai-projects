output "lambda_function_arn" {
  value       = aws_lambda_function.campaign_plan.arn
  description = "Lambda function ARN"
}

output "lambda_function_name" {
  value       = aws_lambda_function.campaign_plan.function_name
  description = "Lambda function name"
}

output "input_queue_arn" {
  value       = aws_sqs_queue.input.arn
  description = "Input SQS FIFO queue ARN (gp-api sends messages here)"
}

output "input_queue_url" {
  value       = aws_sqs_queue.input.url
  description = "Input SQS FIFO queue URL"
}

output "input_queue_name" {
  value       = aws_sqs_queue.input.name
  description = "Input SQS FIFO queue name"
}

output "s3_bucket_name" {
  value       = aws_s3_bucket.results.id
  description = "S3 bucket for campaign plan results"
}

output "dlq_arn" {
  value       = aws_sqs_queue.input_dlq.arn
  description = "Dead letter queue ARN"
}
