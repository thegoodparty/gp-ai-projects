variable "environment" {
  description = "Environment name (dev, qa, prod)"
  type        = string
}

variable "ecs_cluster_arn" {
  description = "ARN of the ECS cluster to run engineer-agent tasks"
  type        = string
  default     = ""
}

variable "ecs_task_definition_arn" {
  description = "ARN of the ECS task definition for engineer-agent"
  type        = string
  default     = ""
}

variable "ecs_task_definition_family" {
  description = "Family name of the ECS task definition (uses latest revision automatically)"
  type        = string
  default     = ""
}

variable "ecs_subnet_ids" {
  description = "Subnet IDs for ECS tasks"
  type        = list(string)
  default     = []
}

variable "ecs_security_group_id" {
  description = "Security group ID for ECS tasks"
  type        = string
  default     = ""
}

variable "ecs_task_execution_role_arn" {
  description = "Task execution role ARN for ECS tasks"
  type        = string
  default     = ""
}

variable "ecs_task_role_arn" {
  description = "Task role ARN for ECS tasks"
  type        = string
  default     = ""
}

variable "enable_fargate_trigger" {
  description = "Whether to enable Fargate task triggering (requires ECS variables to be set)"
  type        = bool
  default     = false
}

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

resource "aws_cloudwatch_log_group" "clickup_bot" {
  name              = "/aws/lambda/clickup-bot-${var.environment}"
  retention_in_days = 30

  tags = {
    Environment = var.environment
    Service     = "clickup-bot"
  }
}

resource "aws_iam_role" "clickup_bot" {
  name = "clickup-bot-lambda-${var.environment}"

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
    Environment = var.environment
    Service     = "clickup-bot"
  }
}

resource "aws_iam_role_policy_attachment" "clickup_bot_basic" {
  role       = aws_iam_role.clickup_bot.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "clickup_bot_secrets" {
  name = "secrets-manager-access"
  role = aws_iam_role.clickup_bot.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue"
        ]
        Resource = "arn:aws:secretsmanager:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:secret:AI_SECRETS_${upper(var.environment)}-??????"
      }
    ]
  })
}

resource "aws_iam_role_policy" "clickup_bot_ecs" {
  count = var.enable_fargate_trigger ? 1 : 0
  name  = "ecs-run-task"
  role  = aws_iam_role.clickup_bot.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "ecs:RunTask"
        ]
        Resource = [
          "arn:aws:ecs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:task-definition/${var.ecs_task_definition_family}:*"
        ]
        Condition = {
          ArnEquals = {
            "ecs:cluster" = var.ecs_cluster_arn
          }
        }
      },
      {
        Effect = "Allow"
        Action = [
          "ecs:TagResource"
        ]
        Resource = [
          "arn:aws:ecs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:task/${element(split("/", var.ecs_cluster_arn), 1)}/*"
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "iam:PassRole"
        ]
        Resource = [
          var.ecs_task_execution_role_arn,
          var.ecs_task_role_arn
        ]
      }
    ]
  })
}

data "archive_file" "lambda_zip" {
  type        = "zip"
  source_file = "${path.module}/../../../clickup_bot/lambda/handler.py"
  output_path = "${path.module}/lambda.zip"
}

resource "aws_lambda_function" "clickup_bot" {
  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256
  function_name    = "clickup-bot-${var.environment}"
  role             = aws_iam_role.clickup_bot.arn
  handler          = "handler.handler"
  runtime          = "python3.12"
  timeout          = 30
  memory_size      = 128

  environment {
    variables = merge(
      {
        ENVIRONMENT = var.environment
      },
      var.enable_fargate_trigger ? {
        ECS_CLUSTER_ARN       = var.ecs_cluster_arn
        ECS_TASK_DEFINITION   = var.ecs_task_definition_family != "" ? var.ecs_task_definition_family : var.ecs_task_definition_arn
        ECS_SUBNET_IDS        = join(",", var.ecs_subnet_ids)
        ECS_SECURITY_GROUP_ID = var.ecs_security_group_id
        ENABLE_FARGATE        = "true"
      } : {}
    )
  }

  depends_on = [aws_cloudwatch_log_group.clickup_bot]

  tags = {
    Environment = var.environment
    Service     = "clickup-bot"
  }
}

output "lambda_function_arn" {
  value       = aws_lambda_function.clickup_bot.arn
  description = "Lambda function ARN"
}

output "lambda_function_name" {
  value       = aws_lambda_function.clickup_bot.function_name
  description = "Lambda function name"
}

output "lambda_invoke_arn" {
  value       = aws_lambda_function.clickup_bot.invoke_arn
  description = "Lambda invoke ARN for API Gateway/ALB"
}
