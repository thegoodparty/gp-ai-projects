output "set_lambda_role_arn" {
  description = "ARN of the SET Lambda IAM role"
  value       = aws_iam_role.set_lambda_role.arn
}

output "retrieve_lambda_role_arn" {
  description = "ARN of the RETRIEVE Lambda IAM role"
  value       = aws_iam_role.retrieve_lambda_role.arn
}

output "set_lambda_credentials_secret_arn" {
  description = "ARN of the Secrets Manager secret containing SET Lambda credentials"
  value       = aws_secretsmanager_secret.set_lambda_credentials.arn
}

output "set_lambda_credentials_secret_name" {
  description = "Name of the Secrets Manager secret containing SET Lambda credentials"
  value       = aws_secretsmanager_secret.set_lambda_credentials.name
}