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
  description = "URL of gp-api's SQS results queue ({stage}-campaign-queue.fifo) that receives experiment results"
  type        = string
}

variable "gp_api_sqs_queue_arn" {
  description = "ARN of gp-api's SQS results queue, used for IAM SendMessage permission"
  type        = string
}

variable "broker_url" {
  description = "HTTPS URL of the broker (e.g. https://broker-dev.ai.goodparty.org). Must be https — the dispatch Lambda carries the service token that mints scope tickets."
  type        = string

  validation {
    condition     = startswith(lower(var.broker_url), "https://")
    error_message = "broker_url must use https:// — dispatch Lambda carries the service token."
  }

  validation {
    condition     = !endswith(var.broker_url, "/")
    error_message = "broker_url must not end with a trailing slash — downstream concatenates paths like ${"/"}anthropic."
  }
}

variable "service_tokens_secret_arn" {
  description = "ARN of the Secrets Manager secret containing service tokens for broker auth"
  type        = string
}

variable "vpc_id" {
  description = "VPC ID for the dispatch Lambda's ENI. Required so the Lambda can resolve the broker hostname via Route53."
  type        = string
}

variable "broker_security_group_id" {
  description = "Security group ID of the broker service. Empty string skips the dispatch Lambda's broker egress rule (Phase 1 bring-up)."
  type        = string
  default     = ""
}

variable "experiment_metadata_bucket_name" {
  description = "Name of the S3 metadata bucket holding PMF experiment manifests + index.json. Injected as EXPERIMENT_METADATA_BUCKET env var. Empty string makes the dispatch Lambda fall back to the bundled DISPATCH_REGISTRY (Phase 1 bring-up before the bucket is provisioned)."
  type        = string
  default     = ""
}

variable "experiment_metadata_read_policy_arn" {
  description = "ARN of the managed IAM policy granting read access to the experiment metadata bucket. Empty string skips the attachment."
  type        = string
  default     = ""
}

variable "max_concurrent_agents" {
  description = "Maximum number of concurrently RUNNING agent Fargate tasks. The dispatch Lambda defers at-cap messages back to the queue with an extended visibility timeout. 0 disables the cap."
  type        = number
  default     = 100
}

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}
data "aws_vpc" "selected" {
  id = var.vpc_id
}

data "aws_secretsmanager_secret_version" "service_tokens" {
  secret_id = var.service_tokens_secret_arn
}

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

  # 30 receives (not 3): cap-deferred messages consume one receive per retry
  # (10-minute visibility per deferral) — this tolerates ~5 hours of cap
  # saturation before a healthy message dead-letters. Tradeoff: genuinely
  # poison messages retry ~60 minutes before reaching the DLQ instead of ~6.
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dispatch_dlq.arn
    maxReceiveCount     = 30
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

resource "aws_iam_role_policy_attachment" "dispatch_lambda_vpc_access" {
  role       = aws_iam_role.dispatch_lambda_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

resource "aws_iam_role_policy_attachment" "dispatch_lambda_experiment_metadata_read" {
  count      = var.experiment_metadata_read_policy_arn != "" ? 1 : 0
  role       = aws_iam_role.dispatch_lambda_role.name
  policy_arn = var.experiment_metadata_read_policy_arn
}

resource "aws_security_group" "dispatch_lambda" {
  name        = "pmf-dispatch-lambda-sg-${var.environment}"
  description = "Dispatch Lambda ENI: egress to broker + AWS APIs via NAT"
  vpc_id      = var.vpc_id

  tags = {
    Name        = "PMF Dispatch Lambda"
    Environment = var.environment
  }
}

resource "aws_security_group_rule" "dispatch_egress_broker" {
  count                    = var.broker_security_group_id != "" ? 1 : 0
  type                     = "egress"
  from_port                = 443
  to_port                  = 443
  protocol                 = "tcp"
  description              = "Reach broker ALB for mint-run-token calls (HTTPS)"
  security_group_id        = aws_security_group.dispatch_lambda.id
  source_security_group_id = var.broker_security_group_id
}

resource "aws_security_group_rule" "dispatch_egress_https" {
  type              = "egress"
  from_port         = 443
  to_port           = 443
  protocol          = "tcp"
  description       = "AWS SDK calls via NAT (SQS, ECS RunTask, Secrets Manager, CloudWatch Logs)"
  security_group_id = aws_security_group.dispatch_lambda.id
  cidr_blocks       = ["0.0.0.0/0"]
}

resource "aws_security_group_rule" "dispatch_egress_dns" {
  type              = "egress"
  from_port         = 53
  to_port           = 53
  protocol          = "udp"
  description       = "DNS resolution via VPC DNS (for the broker hostname)"
  security_group_id = aws_security_group.dispatch_lambda.id
  cidr_blocks       = [data.aws_vpc.selected.cidr_block]
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
          "sqs:GetQueueAttributes",
          "sqs:ChangeMessageVisibility"
        ]
        Resource = aws_sqs_queue.dispatch.arn
      },
      {
        Effect   = "Allow"
        Action   = "ecs:ListTasks"
        Resource = "*"
        Condition = {
          ArnEquals = {
            "ecs:cluster" = var.ecs_cluster_arn
          }
        }
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
        Resource = aws_sqs_queue.results.arn
      },
      {
        Effect = "Allow"
        Action = "secretsmanager:GetSecretValue"
        Resource = [
          "arn:aws:secretsmanager:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:secret:AI_SECRETS_${upper(var.environment)}-*",
          var.service_tokens_secret_arn
        ]
      }
    ]
  })
}

resource "aws_lambda_function" "dispatch" {
  function_name    = "pmf-engine-dispatch-${var.environment}"
  filename         = data.archive_file.dispatch_lambda.output_path
  source_code_hash = data.archive_file.dispatch_lambda.output_base64sha256
  handler          = "dispatch_handler.handler"
  runtime          = "python3.13"
  role             = aws_iam_role.dispatch_lambda_role.arn
  timeout          = 120
  memory_size      = 128

  environment {
    variables = {
      ENVIRONMENT                = var.environment
      ECS_CLUSTER_ARN            = var.ecs_cluster_arn
      ECS_TASK_DEFINITION        = var.ecs_task_definition_family
      ECS_SUBNET_IDS             = join(",", var.ecs_subnet_ids)
      ECS_SECURITY_GROUP_ID      = var.ecs_security_group_id
      ARTIFACT_BUCKET            = aws_s3_bucket.artifacts.id
      CONTAINER_NAME             = "pmf-engine"
      AI_SECRETS_NAME            = "AI_SECRETS_${upper(var.environment)}"
      BROKER_URL                 = var.broker_url
      RESULTS_QUEUE_URL          = aws_sqs_queue.results.url
      SERVICE_TOKEN              = try(jsondecode(data.aws_secretsmanager_secret_version.service_tokens.secret_string)["SERVICE_TOKEN"], "")
      EXPERIMENT_METADATA_BUCKET = var.experiment_metadata_bucket_name
      MAX_CONCURRENT_AGENTS      = tostring(var.max_concurrent_agents)
    }
  }

  vpc_config {
    subnet_ids         = var.ecs_subnet_ids
    security_group_ids = [aws_security_group.dispatch_lambda.id]
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

  # FIFO scales Lambda concurrency to the number of active message groups
  # (one per org), so a bulk dispatch could run hundreds of cap checks
  # simultaneously and race straight past MAX_CONCURRENT_AGENTS. Capping at
  # 2 bounds the check-then-launch race to 1 task of overshoot.
  scaling_config {
    maximum_concurrency = 2
  }
}

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

output "dispatch_lambda_sg_id" {
  value       = aws_security_group.dispatch_lambda.id
  description = "Security group ID attached to the dispatch Lambda's ENI (consumed by broker for ingress rule)"
}
