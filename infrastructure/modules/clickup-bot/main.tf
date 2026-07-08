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

variable "shared_slack_notifier_lambda_arn" {
  description = "ARN of the shared Slack notifier Lambda to subscribe to failure notifications (empty disables)"
  type        = string
  default     = ""
}

variable "failure_notification_email" {
  description = "Email address for failure notifications (empty disables)"
  type        = string
  default     = ""
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

# Seeds the function code at creation time ONLY. After creation, code deploys
# exclusively via .github/workflows/deploy-clickup-bot.yml (aws lambda
# update-function-code); the lifecycle block below stops terraform from
# reverting CI-deployed code on later applies (e.g. from a stale checkout).
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
  runtime          = "python3.13"
  timeout          = 30
  memory_size      = 128

  # Terraform manages config and IAM only. GitHub Actions is the single code
  # writer; without this, any `terraform apply` would upload the applier's
  # local handler.py and could silently roll prod code back.
  lifecycle {
    ignore_changes = [filename, source_code_hash]
  }

  environment {
    variables = merge(
      {
        ENVIRONMENT = var.environment
      },
      var.enable_fargate_trigger ? {
        # Transition compatibility — do NOT remove until the fail-loud handler
        # is confirmed live in prod. The current handler ignores ENABLE_FARGATE
        # (there is no logging-only mode anymore), but the PREVIOUS handler
        # gates on it and silently no-ops (logs HANDOFF_DATA, returns 200)
        # when it is absent. Keeping it means a `terraform apply` that runs
        # before the prod-branch code deploy still restores the old deployed
        # code to a working state instead of re-creating the 12-day silent
        # incident (2026-06-26).
        ENABLE_FARGATE        = "true"
        ECS_CLUSTER_ARN       = var.ecs_cluster_arn
        ECS_TASK_DEFINITION   = var.ecs_task_definition_family != "" ? var.ecs_task_definition_family : var.ecs_task_definition_arn
        ECS_SUBNET_IDS        = join(",", var.ecs_subnet_ids)
        ECS_SECURITY_GROUP_ID = var.ecs_security_group_id
      } : {}
    )
  }

  depends_on = [aws_cloudwatch_log_group.clickup_bot]

  tags = {
    Environment = var.environment
    Service     = "clickup-bot"
  }
}

# Fail-loud is only loud if someone hears it. The handler's failures are
# handled returns (structured 500s/401s), not Lambda invocation errors, so the
# AWS "Errors" metric never fires — the only durable signal is the "ERROR" /
# "Failed to" log lines (including comment posts that post_failure_comment
# deliberately swallows, and signature-verification 401s that would otherwise
# end in ClickUp silently suspending the webhook). This filter + alarm + SNS
# topic turns those lines into Slack/email notifications, mirroring the
# engineer-agent-failures-{env} pattern. Without it, the failures accumulate in
# metrics nobody watches, which is operationally identical to the 12-day
# silence this module exists to prevent.
#
# SECURITY CONTRACT with the handler: this pattern is a substring OR evaluated
# against EVERY log line in the group, and the webhook endpoint is public. The
# handler must therefore never echo unauthenticated request content (raw body,
# headers, event type, history_items) to its logs — otherwise any internet
# client could fire this alarm, or bury it in false positives, by sending
# "ERROR" in a request body. Both sides of the contract are locked by
# clickup_bot/tests/test_handler.py (ALARM_FILTER_TERMS and the alarm-log
# poisoning tests); change the pattern and those tests together.
resource "aws_cloudwatch_log_metric_filter" "handler_errors" {
  name           = "clickup-bot-handler-errors-${var.environment}"
  log_group_name = aws_cloudwatch_log_group.clickup_bot.name
  pattern        = "?\"ERROR\" ?\"Failed to\""

  metric_transformation {
    name          = "HandlerErrors"
    namespace     = "ClickUpBot/${var.environment}"
    value         = "1"
    default_value = "0"
  }
}

resource "aws_sns_topic" "bot_failures" {
  name = "clickup-bot-failures-${var.environment}"

  tags = {
    Name        = "ClickUp Bot Failures"
    Environment = var.environment
    Service     = "clickup-bot"
  }
}

resource "aws_sns_topic_policy" "bot_failures" {
  arn = aws_sns_topic.bot_failures.arn

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "cloudwatch.amazonaws.com"
        }
        Action   = "SNS:Publish"
        Resource = aws_sns_topic.bot_failures.arn
        Condition = {
          StringEquals = {
            "AWS:SourceAccount" = data.aws_caller_identity.current.account_id
          }
        }
      }
    ]
  })
}

resource "aws_sns_topic_subscription" "bot_failures_email" {
  count     = var.failure_notification_email != "" ? 1 : 0
  topic_arn = aws_sns_topic.bot_failures.arn
  protocol  = "email"
  endpoint  = var.failure_notification_email
}

resource "aws_sns_topic_subscription" "shared_slack_notifier" {
  count     = var.shared_slack_notifier_lambda_arn != "" ? 1 : 0
  topic_arn = aws_sns_topic.bot_failures.arn
  protocol  = "lambda"
  endpoint  = var.shared_slack_notifier_lambda_arn
}

resource "aws_lambda_permission" "allow_sns_invoke_slack" {
  count         = var.shared_slack_notifier_lambda_arn != "" ? 1 : 0
  statement_id  = "AllowSNSInvokeFromClickupBotFailures"
  action        = "lambda:InvokeFunction"
  function_name = var.shared_slack_notifier_lambda_arn
  principal     = "sns.amazonaws.com"
  source_arn    = aws_sns_topic.bot_failures.arn
}

resource "aws_cloudwatch_metric_alarm" "handler_errors" {
  alarm_name          = "clickup-bot-handler-errors-${var.environment}"
  alarm_description   = "clickup-bot ${var.environment} logged handler errors (fail-loud 500s or swallowed ClickUp comment failures). Check /aws/lambda/clickup-bot-${var.environment} logs. After any sustained outage, also check the ClickUp webhook health status (see clickup_bot/README.md, 'After an outage')."
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  metric_name         = "HandlerErrors"
  namespace           = "ClickUpBot/${var.environment}"
  period              = 300
  statistic           = "Sum"
  threshold           = 1
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.bot_failures.arn]
  ok_actions          = [aws_sns_topic.bot_failures.arn]

  tags = {
    Environment = var.environment
    Service     = "clickup-bot"
  }
}

output "lambda_function_arn" {
  value       = aws_lambda_function.clickup_bot.arn
  description = "Lambda function ARN"
}

output "failure_sns_topic_arn" {
  value       = aws_sns_topic.bot_failures.arn
  description = "SNS topic that receives clickup-bot handler error alarms"
}

output "lambda_function_name" {
  value       = aws_lambda_function.clickup_bot.function_name
  description = "Lambda function name"
}

output "lambda_invoke_arn" {
  value       = aws_lambda_function.clickup_bot.invoke_arn
  description = "Lambda invoke ARN for API Gateway/ALB"
}
