output "lambda_function_name" {
  description = "Name of the QA Lambda function. Used by CI/CD to update the image."
  value       = aws_lambda_function.qa.function_name
}

output "lambda_function_arn" {
  description = "ARN of the QA Lambda function."
  value       = aws_lambda_function.qa.arn
}

output "queue_url" {
  description = "SQS queue URL. meeting-pipeline's process Lambda reads this via terraform_remote_state and sends QA work here."
  value       = aws_sqs_queue.qa.url
}

output "queue_arn" {
  description = "SQS queue ARN. meeting-pipeline reads this via terraform_remote_state to grant SendMessage permission to its process role."
  value       = aws_sqs_queue.qa.arn
}

output "dlq_arn" {
  description = "Dead-letter queue ARN."
  value       = aws_sqs_queue.qa_dlq.arn
}

output "sns_topic_arn" {
  description = "ARN of the SNS topic that DLQ alarms publish to. Subscribed by the shared Slack notifier (if wired) and any failure_notification_email."
  value       = aws_sns_topic.failures.arn
}
