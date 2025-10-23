output "set_lambda_role_arn" {
  description = "ARN of the SET Lambda IAM role"
  value       = aws_iam_role.set_lambda_role.arn
}

output "retrieve_lambda_role_arn" {
  description = "ARN of the RETRIEVE Lambda IAM role"
  value       = aws_iam_role.retrieve_lambda_role.arn
}

output "set_lambda_user_access_key_id" {
  description = "Access Key ID for SET Lambda user"
  value       = aws_iam_access_key.set_lambda_user_key.id
}

output "set_lambda_user_secret_access_key" {
  description = "Secret Access Key for SET Lambda user"
  value       = aws_iam_access_key.set_lambda_user_key.secret
  sensitive   = true
}