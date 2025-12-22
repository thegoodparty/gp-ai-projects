variable "environment" {
  description = "Environment name (dev, qa, prod)"
  type        = string
}

variable "vpc_id" {
  description = "VPC ID for ECS tasks"
  type        = string
}

variable "private_subnet_ids" {
  description = "Private subnet IDs for ECS tasks"
  type        = list(string)
}

variable "ecr_repository_url" {
  description = "ECR repository URL for gp-ai-projects Docker images"
  type        = string
}

variable "docker_image_tag" {
  description = "Docker image tag for ddhq-matcher"
  type        = string
}

variable "failure_notification_email" {
  description = "Email address to receive ECS task failure notifications"
  type        = string
  default     = ""
}

variable "shared_slack_notifier_lambda_arn" {
  description = "ARN of the shared Slack notifier Lambda function (leave empty to disable Slack notifications)"
  type        = string
  default     = ""
}

variable "task_cpu" {
  description = "CPU units for ECS Fargate task. Valid values: 256, 512, 1024, 2048, 4096, 8192, 16384"
  type        = string
  default     = "4096"

  validation {
    condition     = contains(["256", "512", "1024", "2048", "4096", "8192", "16384"], var.task_cpu)
    error_message = "task_cpu must be one of: 256, 512, 1024, 2048, 4096, 8192, 16384"
  }
}

variable "task_memory" {
  description = "Memory for ECS Fargate task in MB. Valid range depends on CPU (e.g., 4096 CPU supports 8192-30720 MB)"
  type        = string
  default     = "30720"
}

resource "aws_s3_bucket" "matcher_output" {
  bucket = "ddhq-matcher-output-${var.environment}"

  tags = {
    Name        = "DDHQ Matcher Output"
    Environment = var.environment
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "matcher_output" {
  bucket = aws_s3_bucket.matcher_output.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "matcher_output" {
  bucket = aws_s3_bucket.matcher_output.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "matcher_output" {
  bucket = aws_s3_bucket.matcher_output.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "matcher_output_lifecycle" {
  bucket = aws_s3_bucket.matcher_output.id

  rule {
    id     = "archive-outputs"
    status = "Enabled"

    transition {
      days          = 30
      storage_class = "GLACIER"
    }

    filter {
      prefix = "output/"
    }
  }
}

resource "aws_cloudwatch_log_group" "matcher" {
  name              = "/ecs/ddhq-matcher-${var.environment}"
  retention_in_days = 30

  tags = {
    Environment = var.environment
  }
}

resource "aws_cloudwatch_metric_alarm" "matcher_execution_duration" {
  alarm_name          = "ddhq-matcher-long-execution-${var.environment}"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = "1"
  metric_name         = "TaskDuration"
  namespace           = "ECS/Fargate"
  period              = "300"
  statistic           = "Maximum"
  threshold           = "28800"
  alarm_description   = "DDHQ Matcher task running longer than 8 hours (potential stuck/runaway process)"
  alarm_actions       = [aws_sns_topic.matcher_failures.arn]

  dimensions = {
    ClusterName = aws_ecs_cluster.matcher.name
  }

  tags = {
    Environment = var.environment
  }
}

resource "aws_cloudwatch_metric_alarm" "matcher_memory_utilization" {
  alarm_name          = "ddhq-matcher-high-memory-${var.environment}"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = "2"
  metric_name         = "MemoryUtilization"
  namespace           = "AWS/ECS"
  period              = "60"
  statistic           = "Average"
  threshold           = "90"
  alarm_description   = "DDHQ Matcher memory utilization above 90% (potential OOM risk)"
  alarm_actions       = [aws_sns_topic.matcher_failures.arn]

  dimensions = {
    ClusterName = aws_ecs_cluster.matcher.name
    ServiceName = "ddhq-matcher-${var.environment}"
  }

  tags = {
    Environment = var.environment
  }
}

resource "aws_iam_role" "task_execution_role" {
  name = "ddhq-matcher-task-execution-${var.environment}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "ecs-tasks.amazonaws.com"
        }
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "task_execution_role_policy" {
  role       = aws_iam_role.task_execution_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "task_execution_secrets_access" {
  name = "secrets-manager-access"
  role = aws_iam_role.task_execution_role.id

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

resource "aws_iam_role" "task_role" {
  name = "ddhq-matcher-task-${var.environment}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "ecs-tasks.amazonaws.com"
        }
      }
    ]
  })
}

resource "aws_iam_role_policy" "task_s3_access" {
  name = "s3-access"
  role = aws_iam_role.task_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "s3:PutObject",
          "s3:GetObject",
          "s3:ListBucket"
        ]
        Resource = [
          aws_s3_bucket.matcher_output.arn,
          "${aws_s3_bucket.matcher_output.arn}/*"
        ]
      }
    ]
  })
}

resource "aws_security_group" "ecs_tasks" {
  name        = "ddhq-matcher-ecs-tasks-${var.environment}"
  description = "Security group for DDHQ Matcher ECS tasks"
  vpc_id      = var.vpc_id

  egress {
    description = "HTTPS for AWS APIs, Gemini API, Databricks"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "DNS resolution"
    from_port   = 53
    to_port     = 53
    protocol    = "udp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name        = "DDHQ Matcher ECS Tasks"
    Environment = var.environment
  }
}

resource "aws_ecs_task_definition" "matcher" {
  family                   = "ddhq-matcher-${var.environment}"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.task_execution_role.arn
  task_role_arn            = aws_iam_role.task_role.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }

  container_definitions = jsonencode([
    {
      name  = "ddhq-matcher"
      image = "${var.ecr_repository_url}:${var.docker_image_tag}"

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.matcher.name
          "awslogs-region"        = data.aws_region.current.name
          "awslogs-stream-prefix" = "ecs"
        }
      }

      secrets = [
        {
          name      = "GEMINI_API_KEY"
          valueFrom = "arn:aws:secretsmanager:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:secret:AI_SECRETS_${upper(var.environment)}:GEMINI_API_KEY::"
        },
        {
          name      = "DATABRICKS_API_KEY"
          valueFrom = "arn:aws:secretsmanager:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:secret:AI_SECRETS_${upper(var.environment)}:DATABRICKS_API_KEY::"
        },
        {
          name      = "DATABRICKS_SERVER_HOSTNAME"
          valueFrom = "arn:aws:secretsmanager:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:secret:AI_SECRETS_${upper(var.environment)}:DATABRICKS_SERVER_HOSTNAME::"
        },
        {
          name      = "DATABRICKS_HTTP_PATH"
          valueFrom = "arn:aws:secretsmanager:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:secret:AI_SECRETS_${upper(var.environment)}:DATABRICKS_HTTP_PATH::"
        },
        {
          name      = "DATABRICKS_CATALOG"
          valueFrom = "arn:aws:secretsmanager:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:secret:AI_SECRETS_${upper(var.environment)}:DATABRICKS_CATALOG::"
        },
        {
          name      = "DATABRICKS_SCHEMA"
          valueFrom = "arn:aws:secretsmanager:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:secret:AI_SECRETS_${upper(var.environment)}:DATABRICKS_SCHEMA::"
        }
      ]

      environment = [
        {
          name  = "ENVIRONMENT"
          value = var.environment
        },
        {
          name  = "S3_OUTPUT_BUCKET"
          value = aws_s3_bucket.matcher_output.id
        }
      ]
    }
  ])

  tags = {
    Environment = var.environment
  }
}

resource "aws_ecs_cluster" "matcher" {
  name = "ddhq-matcher-${var.environment}"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }

  tags = {
    Environment = var.environment
  }
}

resource "aws_iam_role" "lambda_trigger" {
  name = "ddhq-matcher-lambda-trigger-${var.environment}"

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

resource "aws_iam_role_policy_attachment" "lambda_basic" {
  role       = aws_iam_role.lambda_trigger.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "lambda_ecs_run_task" {
  name = "ecs-run-task"
  role = aws_iam_role.lambda_trigger.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "ecs:RunTask"
        ]
        Resource = [
          aws_ecs_task_definition.matcher.arn,
          "${aws_ecs_task_definition.matcher.arn}:*"
        ]
        Condition = {
          ArnEquals = {
            "ecs:cluster" = aws_ecs_cluster.matcher.arn
          }
        }
      },
      {
        Effect = "Allow"
        Action = [
          "iam:PassRole"
        ]
        Resource = [
          aws_iam_role.task_execution_role.arn,
          aws_iam_role.task_role.arn
        ]
      }
    ]
  })
}

data "archive_file" "lambda_zip" {
  type        = "zip"
  source_file = "${path.module}/lambda-trigger/lambda_handler.py"
  output_path = "${path.module}/lambda-trigger.zip"
}

resource "aws_lambda_function" "matcher_trigger" {
  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256
  function_name    = "ddhq-matcher-trigger-${var.environment}"
  role             = aws_iam_role.lambda_trigger.arn
  handler          = "lambda_handler.handler"
  runtime          = "python3.12"
  timeout          = 30

  environment {
    variables = {
      ECS_CLUSTER_NAME     = aws_ecs_cluster.matcher.name
      TASK_DEFINITION_ARN  = aws_ecs_task_definition.matcher.arn
      SUBNET_IDS           = join(",", var.private_subnet_ids)
      SECURITY_GROUP_ID    = aws_security_group.ecs_tasks.id
      S3_OUTPUT_BUCKET     = aws_s3_bucket.matcher_output.id
    }
  }

  tags = {
    Environment = var.environment
  }
}

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

output "cluster_name" {
  value       = aws_ecs_cluster.matcher.name
  description = "ECS cluster name"
}

output "task_definition_arn" {
  value       = aws_ecs_task_definition.matcher.arn
  description = "ECS task definition ARN"
}

output "lambda_function_arn" {
  value       = aws_lambda_function.matcher_trigger.arn
  description = "Lambda trigger function ARN"
}

output "lambda_function_name" {
  value       = aws_lambda_function.matcher_trigger.function_name
  description = "Lambda trigger function name"
}

output "s3_bucket_name" {
  value       = aws_s3_bucket.matcher_output.id
  description = "S3 bucket for matcher output"
}

output "security_group_id" {
  value       = aws_security_group.ecs_tasks.id
  description = "Security group ID for ECS tasks"
}

resource "aws_sns_topic" "matcher_failures" {
  name = "ddhq-matcher-failures-${var.environment}"

  tags = {
    Name        = "DDHQ Matcher Failures"
    Environment = var.environment
  }
}

resource "aws_sns_topic_subscription" "matcher_failures_email" {
  count     = var.failure_notification_email != "" ? 1 : 0
  topic_arn = aws_sns_topic.matcher_failures.arn
  protocol  = "email"
  endpoint  = var.failure_notification_email
}

resource "aws_cloudwatch_event_rule" "ecs_task_failed" {
  name        = "ddhq-matcher-task-failed-${var.environment}"
  description = "Capture ECS task failures for DDHQ matcher"

  event_pattern = jsonencode({
    source      = ["aws.ecs"]
    detail-type = ["ECS Task State Change"]
    detail = {
      clusterArn = [aws_ecs_cluster.matcher.arn]
      lastStatus = ["STOPPED"]
      containers = {
        exitCode = [{
          "anything-but" = 0
        }]
      }
    }
  })

  tags = {
    Environment = var.environment
  }
}

resource "aws_cloudwatch_event_target" "send_to_sns" {
  rule      = aws_cloudwatch_event_rule.ecs_task_failed.name
  target_id = "SendToSNS"
  arn       = aws_sns_topic.matcher_failures.arn

  input_transformer {
    input_paths = {
      taskArn       = "$.detail.taskArn"
      stoppedReason = "$.detail.stoppedReason"
      exitCode      = "$.detail.containers[0].exitCode"
      clusterArn    = "$.detail.clusterArn"
      time          = "$.time"
    }

    input_template = <<EOF
{
  "alarm": "🔴 DDHQ Matcher Pipeline Failed",
  "pipeline": "HubSpot-DDHQ Race Matching",
  "environment": "${var.environment}",
  "cluster": <clusterArn>,
  "taskArn": <taskArn>,
  "stoppedReason": <stoppedReason>,
  "exitCode": <exitCode>,
  "time": <time>,
  "logs": "https://console.aws.amazon.com/cloudwatch/home?region=${data.aws_region.current.name}#logsV2:log-groups/log-group/$252Fecs$252Fddhq-matcher-${var.environment}",
  "s3_bucket": "${aws_s3_bucket.matcher_output.id}",
  "description": "DDHQ Matcher pipeline failed during execution. Check CloudWatch logs for detailed error information."
}
EOF
  }
}

resource "aws_sns_topic_policy" "matcher_failures" {
  arn = aws_sns_topic.matcher_failures.arn

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "events.amazonaws.com"
        }
        Action   = "SNS:Publish"
        Resource = aws_sns_topic.matcher_failures.arn
      }
    ]
  })
}

resource "aws_sns_topic_subscription" "shared_slack_notifier" {
  count     = var.shared_slack_notifier_lambda_arn != "" ? 1 : 0
  topic_arn = aws_sns_topic.matcher_failures.arn
  protocol  = "lambda"
  endpoint  = var.shared_slack_notifier_lambda_arn
}

resource "aws_lambda_permission" "allow_sns_invoke_slack" {
  count         = var.shared_slack_notifier_lambda_arn != "" ? 1 : 0
  statement_id  = "AllowSNSInvokeFromMatcherFailures"
  action        = "lambda:InvokeFunction"
  function_name = var.shared_slack_notifier_lambda_arn
  principal     = "sns.amazonaws.com"
  source_arn    = aws_sns_topic.matcher_failures.arn
}

output "sns_topic_arn" {
  value       = aws_sns_topic.matcher_failures.arn
  description = "SNS topic for matcher failure notifications"
}
