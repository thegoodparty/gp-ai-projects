data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

locals {
  function_name    = "campaign-plan-${var.environment}"
  input_queue_name = "campaign-plan-input-${var.environment}.fifo"
  dlq_name         = "campaign-plan-dlq-${var.environment}.fifo"
  s3_bucket_name   = "campaign-plan-results-${var.environment}"
}

# ===== CloudWatch Logs =====

resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${local.function_name}"
  retention_in_days = 90

  tags = {
    Environment = var.environment
    Project     = "campaign-plan"
  }
}

# ===== S3 Bucket for Results =====

resource "aws_s3_bucket" "results" {
  bucket = local.s3_bucket_name

  tags = {
    Environment = var.environment
    Project     = "campaign-plan"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "results" {
  bucket = aws_s3_bucket.results.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "results" {
  bucket                  = aws_s3_bucket.results.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "results" {
  bucket = aws_s3_bucket.results.id

  rule {
    id     = "expire-results-12-months"
    status = "Enabled"

    filter {
      prefix = "results/"
    }

    expiration {
      days = 365
    }
  }
}

# ===== SQS Input Queue (FIFO) =====

resource "aws_sqs_queue" "input_dlq" {
  name                        = local.dlq_name
  fifo_queue                  = true
  content_based_deduplication = true
  message_retention_seconds   = 1209600 # 14 days (SQS maximum)

  tags = {
    Environment = var.environment
    Project     = "campaign-plan"
  }
}

resource "aws_sqs_queue" "input" {
  name                       = local.input_queue_name
  fifo_queue                 = true
  content_based_deduplication = false
  visibility_timeout_seconds = 960  # 16 minutes (> Lambda 15 min timeout)
  message_retention_seconds  = 1209600 # 14 days (SQS maximum)
  receive_wait_time_seconds  = 20    # Long polling

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.input_dlq.arn
    maxReceiveCount     = 3 # Try 3 times, then DLQ
  })

  tags = {
    Environment = var.environment
    Project     = "campaign-plan"
  }
}

# ===== IAM Role =====

resource "aws_iam_role" "lambda" {
  name = "campaign-plan-lambda-${var.environment}"

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
    Project     = "campaign-plan"
  }
}

resource "aws_iam_role_policy_attachment" "lambda_basic" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# Secrets Manager access
resource "aws_iam_role_policy" "secrets" {
  name = "secrets-manager-access"
  role = aws_iam_role.lambda.id

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

# SQS access (read input queue, write to gp-api's output queue)
resource "aws_iam_role_policy" "sqs" {
  name = "sqs-access"
  role = aws_iam_role.lambda.id

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
        Resource = aws_sqs_queue.input.arn
      },
      {
        Effect   = "Allow"
        Action   = ["sqs:SendMessage"]
        Resource = var.output_sqs_queue_arn
      }
    ]
  })
}

# S3 access
resource "aws_iam_role_policy" "s3" {
  name = "s3-access"
  role = aws_iam_role.lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = "${aws_s3_bucket.results.arn}/results/*"
      }
    ]
  })
}

# ===== Lambda Function =====

resource "aws_lambda_function" "campaign_plan" {
  filename         = "${path.module}/../../../campaign_plan_lambda/lambda.zip"
  source_code_hash = fileexists("${path.module}/../../../campaign_plan_lambda/lambda.zip") ? filebase64sha256("${path.module}/../../../campaign_plan_lambda/lambda.zip") : null
  function_name    = local.function_name
  role             = aws_iam_role.lambda.arn
  handler          = "handler.handler"
  runtime          = "python3.12"
  timeout          = 900  # 15 minutes (maximum)
  memory_size      = 1024 # 1 GB

  environment {
    variables = {
      ENVIRONMENT          = var.environment
      S3_RESULTS_BUCKET    = aws_s3_bucket.results.id
      OUTPUT_SQS_QUEUE_URL = var.output_sqs_queue_url
    }
  }

  depends_on = [aws_cloudwatch_log_group.lambda]

  tags = {
    Environment = var.environment
    Project     = "campaign-plan"
  }
}

# ===== SQS Trigger =====

resource "aws_lambda_event_source_mapping" "sqs_trigger" {
  event_source_arn = aws_sqs_queue.input.arn
  function_name    = aws_lambda_function.campaign_plan.arn
  batch_size       = 1 # Process one campaign at a time
  enabled          = true
}

# ===== Failure Notifications =====

resource "aws_sns_topic" "failures" {
  name = "campaign-plan-failures-${var.environment}"

  tags = {
    Environment = var.environment
    Project     = "campaign-plan"
  }
}

resource "aws_sns_topic_policy" "failures" {
  arn = aws_sns_topic.failures.arn

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "cloudwatch.amazonaws.com"
        }
        Action   = "SNS:Publish"
        Resource = aws_sns_topic.failures.arn
      }
    ]
  })
}

# Email subscription (optional)
resource "aws_sns_topic_subscription" "failures_email" {
  count     = var.failure_notification_email != "" ? 1 : 0
  topic_arn = aws_sns_topic.failures.arn
  protocol  = "email"
  endpoint  = var.failure_notification_email
}

# Slack notifier subscription (optional)
resource "aws_sns_topic_subscription" "shared_slack_notifier" {
  count     = var.shared_slack_notifier_lambda_arn != "" ? 1 : 0
  topic_arn = aws_sns_topic.failures.arn
  protocol  = "lambda"
  endpoint  = var.shared_slack_notifier_lambda_arn
}

# DLQ depth alarm — fires when any message lands in the DLQ
resource "aws_cloudwatch_metric_alarm" "dlq_messages" {
  alarm_name          = "campaign-plan-dlq-messages-${var.environment}"
  alarm_description   = "Campaign plan generation failed after all retries (message in DLQ)"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ApproximateNumberOfMessagesVisible"
  namespace           = "AWS/SQS"
  period              = 300 # 5 minutes
  statistic           = "Maximum"
  threshold           = 0
  treat_missing_data  = "notBreaching"

  dimensions = {
    QueueName = aws_sqs_queue.input_dlq.name
  }

  alarm_actions = [aws_sns_topic.failures.arn]

  tags = {
    Environment = var.environment
    Project     = "campaign-plan"
  }
}
