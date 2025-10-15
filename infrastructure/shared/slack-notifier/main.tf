variable "environment" {
  description = "Environment (dev/qa/prod)"
  type        = string
  validation {
    condition     = contains(["dev", "qa", "prod"], var.environment)
    error_message = "Environment must be 'dev', 'qa', or 'prod'."
  }
}

variable "allowed_sns_topic_arns" {
  description = "List of SNS topic ARNs allowed to invoke this Lambda"
  type        = list(string)
  default     = []
}

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

# IAM Role for Lambda
resource "aws_iam_role" "slack_notifier" {
  name = "shared-slack-notifier"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "lambda.amazonaws.com"
        }
      }
    ]
  })

  tags = {
    Name        = "Shared Slack Notifier"
    ManagedBy   = "Terraform"
  }
}

resource "aws_iam_role_policy_attachment" "slack_notifier_basic" {
  role       = aws_iam_role.slack_notifier.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "slack_notifier_secrets_access" {
  name = "secrets-manager-access"
  role = aws_iam_role.slack_notifier.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue"
        ]
        Resource = "arn:aws:secretsmanager:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:secret:AI_SECRETS_${upper(var.environment)}-*"
      }
    ]
  })
}

# Lambda Function
resource "aws_lambda_function" "slack_notifier" {
  filename      = "${path.module}/slack-notifier.zip"
  function_name = "shared-slack-notifier"
  role          = aws_iam_role.slack_notifier.arn
  handler       = "index.handler"
  runtime       = "nodejs22.x"
  timeout       = 30
  source_code_hash = filebase64sha256("${path.module}/slack-notifier.zip")

  environment {
    variables = {
      SECRET_NAME    = "AI_SECRETS_${upper(var.environment)}"
      SECRET_REGION  = data.aws_region.current.name
    }
  }

  tags = {
    Name        = "Shared Slack Notifier"
    ManagedBy   = "Terraform"
  }
}

# CloudWatch Log Group
resource "aws_cloudwatch_log_group" "slack_notifier" {
  name              = "/aws/lambda/${aws_lambda_function.slack_notifier.function_name}"
  retention_in_days = 7

  tags = {
    Name        = "Shared Slack Notifier Logs"
    ManagedBy   = "Terraform"
  }
}

# Lambda permission for SNS - allow any SNS topic in the account
resource "aws_lambda_permission" "allow_sns" {
  statement_id  = "AllowExecutionFromSNS"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.slack_notifier.function_name
  principal     = "sns.amazonaws.com"
  # Allow any SNS topic in this account to invoke
  source_arn    = "arn:aws:sns:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:*"
}

# Outputs
output "lambda_function_arn" {
  description = "ARN of the shared Slack notifier Lambda function"
  value       = aws_lambda_function.slack_notifier.arn
}

output "lambda_function_name" {
  description = "Name of the shared Slack notifier Lambda function"
  value       = aws_lambda_function.slack_notifier.function_name
}
