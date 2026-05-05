output "scan_lambda_arn" {
  value = aws_lambda_function.scan.arn
}

output "scan_lambda_name" {
  value = aws_lambda_function.scan.function_name
}

output "process_lambda_arn" {
  value = aws_lambda_function.process.arn
}

output "process_lambda_name" {
  value = aws_lambda_function.process.function_name
}

output "process_queue_url" {
  value = aws_sqs_queue.process.url
}

output "process_queue_arn" {
  value = aws_sqs_queue.process.arn
}

output "discover_queue_url" {
  value = aws_sqs_queue.discover.url
}

output "step_function_arn" {
  value = aws_sfn_state_machine.scan_fanout.arn
}

output "sns_topic_arn" {
  value = aws_sns_topic.failures.arn
}

output "ecs_cluster_name" {
  value = aws_ecs_cluster.discover.name
}

output "ecs_task_definition_arn" {
  value = aws_ecs_task_definition.discover.arn
}

output "ecs_service_name" {
  value = aws_ecs_service.discover.name
}
