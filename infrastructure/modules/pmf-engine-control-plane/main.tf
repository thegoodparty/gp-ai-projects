variable "environment" {
  description = "Environment name (dev, qa, prod)"
  type        = string
}

variable "ecs_cluster_arn" {
  description = "ARN of the PMF engine ECS cluster"
  type        = string
}

variable "ecs_task_definition_family" {
  description = "ECS task definition family for the PMF engine runner"
  type        = string
}

variable "ecs_subnet_ids" {
  description = "Subnet IDs for ECS task networking"
  type        = list(string)
}

variable "ecs_security_group_id" {
  description = "Security group ID for ECS tasks"
  type        = string
}

variable "ecs_task_execution_role_arn" {
  description = "Task execution role ARN (for IAM PassRole)"
  type        = string
}

variable "ecs_task_role_arn" {
  description = "Task role ARN (for IAM PassRole)"
  type        = string
}

variable "lambda_package_dir" {
  description = "Path to the built Lambda package directory (run pmf_engine/scripts/build_lambda_package.sh first)"
  type        = string
}

variable "sns_topic_arn" {
  description = "ARN of the SNS topic for failure notifications (from Fargate module)"
  type        = string
  default     = ""
}

variable "gp_api_sqs_queue_url" {
  description = "URL of gp-api's SQS results queue ({stage}-campaign-queue.fifo) that the callback Lambda forwards experiment results into"
  type        = string
}

variable "gp_api_sqs_queue_arn" {
  description = "ARN of gp-api's SQS results queue, used for IAM SendMessage permission on the callback Lambda role"
  type        = string
}

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

# --- S3: Artifact Bucket ---

resource "aws_s3_bucket" "artifacts" {
  bucket = "gp-agent-artifacts-${var.environment}"

  tags = {
    Name        = "GP Agent Artifacts"
    Environment = var.environment
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  rule {
    id     = "transition-to-ia"
    status = "Enabled"

    filter {}

    transition {
      days          = 90
      storage_class = "STANDARD_IA"
    }
  }

  # Run logs (workspace, /tmp scratch, redacted session jsonl) are tagged
  # `lifecycle=logs` by the runner (_upload_run_logs in runner/main.py). This
  # rule only expires tagged objects, so canonical artifacts
  # (e.g. district_intel/latest.json consumed by peer_city_benchmarking) are
  # preserved indefinitely. Matching on key prefix cannot distinguish logs from
  # artifacts because both share the `{experiment_id}/{run_id}/` prefix.
  rule {
    id     = "expire-run-logs"
    status = "Enabled"

    filter {
      tag {
        key   = "lifecycle"
        value = "logs"
      }
    }

    expiration {
      days = 365
    }
  }
}


# --- SQS: Dispatch Queue ---

resource "aws_sqs_queue" "dispatch_dlq" {
  name                        = "agent-dispatch-dlq-${var.environment}.fifo"
  fifo_queue                  = true
  message_retention_seconds   = 604800
  content_based_deduplication = true

  tags = {
    Environment = var.environment
  }
}

resource "aws_sqs_queue" "dispatch" {
  name                        = "agent-dispatch-${var.environment}.fifo"
  fifo_queue                  = true
  visibility_timeout_seconds  = 120
  message_retention_seconds   = 604800
  content_based_deduplication = false
  deduplication_scope         = "messageGroup"
  fifo_throughput_limit       = "perMessageGroupId"

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dispatch_dlq.arn
    maxReceiveCount     = 3
  })

  tags = {
    Environment = var.environment
  }
}

# --- SQS: Callback Queue ---

resource "aws_sqs_queue" "callback_dlq" {
  name                        = "agent-callback-dlq-${var.environment}.fifo"
  fifo_queue                  = true
  message_retention_seconds   = 604800
  content_based_deduplication = true

  tags = {
    Environment = var.environment
  }
}

resource "aws_sqs_queue" "callback" {
  name                        = "agent-callback-${var.environment}.fifo"
  fifo_queue                  = true
  visibility_timeout_seconds  = 120
  message_retention_seconds   = 604800
  content_based_deduplication = false
  deduplication_scope         = "messageGroup"
  fifo_throughput_limit       = "perMessageGroupId"

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.callback_dlq.arn
    maxReceiveCount     = 3
  })

  tags = {
    Environment = var.environment
  }
}

# --- SQS: Results Queue (consumed by gp-api) ---

resource "aws_sqs_queue" "results_dlq" {
  name                        = "agent-results-dlq-${var.environment}.fifo"
  fifo_queue                  = true
  message_retention_seconds   = 604800
  content_based_deduplication = true

  tags = {
    Environment = var.environment
  }
}

resource "aws_sqs_queue" "results" {
  name                        = "agent-results-${var.environment}.fifo"
  fifo_queue                  = true
  visibility_timeout_seconds  = 300
  message_retention_seconds   = 604800
  content_based_deduplication = false
  deduplication_scope         = "messageGroup"
  fifo_throughput_limit       = "perMessageGroupId"

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.results_dlq.arn
    maxReceiveCount     = 3
  })

  tags = {
    Environment = var.environment
  }
}

# --- Lambda: Dispatch Handler ---

data "archive_file" "dispatch_lambda" {
  type        = "zip"
  source_dir  = var.lambda_package_dir
  output_path = "${path.module}/dispatch_lambda.zip"
}

resource "aws_iam_role" "dispatch_lambda_role" {
  name = "pmf-engine-dispatch-lambda-${var.environment}"

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
}

resource "aws_iam_role_policy_attachment" "dispatch_lambda_basic" {
  role       = aws_iam_role.dispatch_lambda_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "dispatch_lambda_permissions" {
  name = "dispatch-permissions"
  role = aws_iam_role.dispatch_lambda_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes"
        ]
        Resource = aws_sqs_queue.dispatch.arn
      },
      {
        Effect   = "Allow"
        Action   = "ecs:RunTask"
        Resource = "arn:aws:ecs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:task-definition/${var.ecs_task_definition_family}:*"
        Condition = {
          ArnEquals = {
            "ecs:cluster" = var.ecs_cluster_arn
          }
        }
      },
      {
        Effect = "Allow"
        Action = "iam:PassRole"
        Resource = [
          var.ecs_task_execution_role_arn,
          var.ecs_task_role_arn
        ]
      },
      {
        Effect   = "Allow"
        Action   = "cloudwatch:PutMetricData"
        Resource = "*"
        Condition = {
          StringEquals = {
            "cloudwatch:namespace" = "PMFEngine"
          }
        }
      },
      {
        Effect   = "Allow"
        Action   = "sqs:SendMessage"
        Resource = aws_sqs_queue.callback.arn
      },
      {
        Effect = "Allow"
        Action = "secretsmanager:GetSecretValue"
        # Used by param_screening.py (_load_gemini_api_key) which calls the
        # Gemini prompt-injection screener during dispatch. GEMINI_API_KEY is
        # read from AI_SECRETS_{ENV} when not already set in the env. Removing
        # this permission disables the LLM-based screener and falls back to
        # structural checks only.
        Resource = "arn:aws:secretsmanager:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:secret:AI_SECRETS_${upper(var.environment)}-*"
      }
    ]
  })
}

resource "aws_lambda_function" "dispatch" {
  function_name    = "pmf-engine-dispatch-${var.environment}"
  filename         = data.archive_file.dispatch_lambda.output_path
  source_code_hash = data.archive_file.dispatch_lambda.output_base64sha256
  handler          = "dispatch_handler.handler"
  runtime          = "python3.12"
  role             = aws_iam_role.dispatch_lambda_role.arn
  timeout          = 120
  memory_size      = 128

  environment {
    variables = {
      ENVIRONMENT           = var.environment
      ECS_CLUSTER_ARN       = var.ecs_cluster_arn
      ECS_TASK_DEFINITION   = var.ecs_task_definition_family
      ECS_SUBNET_IDS        = join(",", var.ecs_subnet_ids)
      ECS_SECURITY_GROUP_ID = var.ecs_security_group_id
      ARTIFACT_BUCKET       = aws_s3_bucket.artifacts.id
      CALLBACK_QUEUE_URL    = aws_sqs_queue.callback.url
      CONTAINER_NAME        = "pmf-engine"
      AI_SECRETS_NAME       = "AI_SECRETS_${upper(var.environment)}"
    }
  }

  tags = {
    Environment = var.environment
  }
}

resource "aws_lambda_event_source_mapping" "dispatch_sqs" {
  event_source_arn = aws_sqs_queue.dispatch.arn
  function_name    = aws_lambda_function.dispatch.arn
  batch_size       = 1
  enabled          = true

  function_response_types = ["ReportBatchItemFailures"]
}

# --- Lambda: Callback Handler ---

data "archive_file" "callback_lambda" {
  type        = "zip"
  source_dir  = var.lambda_package_dir
  output_path = "${path.module}/callback_lambda.zip"
}

resource "aws_iam_role" "callback_lambda_role" {
  name = "pmf-engine-callback-lambda-${var.environment}"

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
}

resource "aws_iam_role_policy_attachment" "callback_lambda_basic" {
  role       = aws_iam_role.callback_lambda_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "callback_lambda_permissions" {
  name = "callback-permissions"
  role = aws_iam_role.callback_lambda_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes"
        ]
        Resource = aws_sqs_queue.callback.arn
      },
      {
        Effect   = "Allow"
        Action   = "sqs:SendMessage"
        Resource = var.gp_api_sqs_queue_arn
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:HeadObject"]
        Resource = "${aws_s3_bucket.artifacts.arn}/*"
      }
    ]
  })
}

resource "aws_lambda_function" "callback" {
  function_name    = "pmf-engine-callback-${var.environment}"
  filename         = data.archive_file.callback_lambda.output_path
  source_code_hash = data.archive_file.callback_lambda.output_base64sha256
  handler          = "callback_handler.handler"
  runtime          = "python3.12"
  role             = aws_iam_role.callback_lambda_role.arn
  timeout          = 60
  memory_size      = 128

  environment {
    variables = {
      ENVIRONMENT       = var.environment
      ARTIFACT_BUCKET   = aws_s3_bucket.artifacts.id
      RESULTS_QUEUE_URL = var.gp_api_sqs_queue_url
    }
  }

  tags = {
    Environment = var.environment
  }
}

resource "aws_lambda_event_source_mapping" "callback_sqs" {
  event_source_arn = aws_sqs_queue.callback.arn
  function_name    = aws_lambda_function.callback.arn
  batch_size       = 1
  enabled          = true

  function_response_types = ["ReportBatchItemFailures"]
}

# --- Outputs ---

# --- CloudWatch Alarms ---

resource "aws_cloudwatch_metric_alarm" "dispatch_lambda_errors" {
  count               = var.sns_topic_arn != "" ? 1 : 0
  alarm_name          = "pmf-engine-dispatch-lambda-errors-${var.environment}"
  alarm_description   = "PMF Engine dispatch Lambda errors"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  alarm_actions       = [var.sns_topic_arn]

  dimensions = {
    FunctionName = aws_lambda_function.dispatch.function_name
  }

  tags = {
    Environment = var.environment
  }
}

resource "aws_cloudwatch_metric_alarm" "callback_lambda_errors" {
  count               = var.sns_topic_arn != "" ? 1 : 0
  alarm_name          = "pmf-engine-callback-lambda-errors-${var.environment}"
  alarm_description   = "PMF Engine callback Lambda errors"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  alarm_actions       = [var.sns_topic_arn]

  dimensions = {
    FunctionName = aws_lambda_function.callback.function_name
  }

  tags = {
    Environment = var.environment
  }
}

resource "aws_cloudwatch_metric_alarm" "dispatch_dlq_depth" {
  count               = var.sns_topic_arn != "" ? 1 : 0
  alarm_name          = "pmf-engine-dispatch-dlq-${var.environment}"
  alarm_description   = "Messages in PMF Engine dispatch DLQ"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ApproximateNumberOfMessagesVisible"
  namespace           = "AWS/SQS"
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  alarm_actions       = [var.sns_topic_arn]

  dimensions = {
    QueueName = aws_sqs_queue.dispatch_dlq.name
  }

  tags = {
    Environment = var.environment
  }
}

resource "aws_cloudwatch_metric_alarm" "callback_dlq_depth" {
  count               = var.sns_topic_arn != "" ? 1 : 0
  alarm_name          = "pmf-engine-callback-dlq-${var.environment}"
  alarm_description   = "Messages in PMF Engine callback DLQ"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ApproximateNumberOfMessagesVisible"
  namespace           = "AWS/SQS"
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  alarm_actions       = [var.sns_topic_arn]

  dimensions = {
    QueueName = aws_sqs_queue.callback_dlq.name
  }

  tags = {
    Environment = var.environment
  }
}

resource "aws_cloudwatch_metric_alarm" "results_dlq_depth" {
  count               = var.sns_topic_arn != "" ? 1 : 0
  alarm_name          = "pmf-engine-results-dlq-${var.environment}"
  alarm_description   = "Messages in PMF Engine results DLQ"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ApproximateNumberOfMessagesVisible"
  namespace           = "AWS/SQS"
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  alarm_actions       = [var.sns_topic_arn]

  dimensions = {
    QueueName = aws_sqs_queue.results_dlq.name
  }

  tags = {
    Environment = var.environment
  }
}

resource "aws_cloudwatch_metric_alarm" "param_screening_bypassed" {
  count               = var.sns_topic_arn != "" ? 1 : 0
  alarm_name          = "pmf-engine-param-screening-bypassed-${var.environment}"
  alarm_description   = "PMF Engine LLM param screening is being bypassed (API key missing or Gemini errors)"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ParamScreeningBypassed"
  namespace           = "PMFEngine"
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  alarm_actions       = [var.sns_topic_arn]

  dimensions = {
    Environment = var.environment
  }

  tags = {
    Environment = var.environment
  }
}

resource "aws_cloudwatch_metric_alarm" "param_screening_rejected" {
  count               = var.sns_topic_arn != "" ? 1 : 0
  alarm_name          = "pmf-engine-param-screening-rejected-${var.environment}"
  alarm_description   = "PMF Engine param screening rejected >5 suspicious inputs in 1 hour"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ParamScreeningRejected"
  namespace           = "PMFEngine"
  period              = 3600
  statistic           = "Sum"
  threshold           = 5
  alarm_actions       = [var.sns_topic_arn]

  dimensions = {
    Environment = var.environment
  }

  tags = {
    Environment = var.environment
  }
}

# --- Outputs ---

output "dispatch_queue_url" {
  value       = aws_sqs_queue.dispatch.url
  description = "URL of the agent dispatch FIFO queue"
}

output "dispatch_queue_arn" {
  value       = aws_sqs_queue.dispatch.arn
  description = "ARN of the agent dispatch FIFO queue"
}

output "callback_queue_url" {
  value       = aws_sqs_queue.callback.url
  description = "URL of the agent callback FIFO queue"
}

output "callback_queue_arn" {
  value       = aws_sqs_queue.callback.arn
  description = "ARN of the agent callback FIFO queue"
}

output "results_queue_url" {
  value       = aws_sqs_queue.results.url
  description = "URL of the agent results FIFO queue (consumed by gp-api)"
}

output "results_queue_arn" {
  value       = aws_sqs_queue.results.arn
  description = "ARN of the agent results FIFO queue"
}

output "artifact_bucket_name" {
  value       = aws_s3_bucket.artifacts.id
  description = "Name of the artifacts S3 bucket"
}

output "artifact_bucket_arn" {
  value       = aws_s3_bucket.artifacts.arn
  description = "ARN of the artifacts S3 bucket"
}
