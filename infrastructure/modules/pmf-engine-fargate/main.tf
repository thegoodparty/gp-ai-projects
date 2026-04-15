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
  description = "Docker image tag for pmf-engine"
  type        = string
  default     = "pmf-engine-prod"
}

variable "failure_notification_email" {
  description = "Email address to receive ECS task failure notifications"
  type        = string
  default     = ""
}

variable "shared_slack_notifier_lambda_arn" {
  description = "ARN of the shared Slack notifier Lambda function"
  type        = string
  default     = ""
}

variable "task_cpu" {
  description = "CPU units for ECS Fargate task"
  type        = string
  default     = "1024"
}

variable "task_memory" {
  description = "Memory for ECS Fargate task in MB"
  type        = string
  default     = "2048"
}

variable "artifact_bucket_arn" {
  description = "ARN of the S3 artifacts bucket"
  type        = string
}

variable "callback_queue_arn" {
  description = "ARN of the agent-callback SQS queue"
  type        = string
}

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

resource "aws_cloudwatch_log_group" "runner" {
  name              = "/ecs/pmf-engine-${var.environment}"
  retention_in_days = 30

  tags = {
    Environment = var.environment
  }
}

resource "aws_iam_role" "task_execution_role" {
  name = "pmf-engine-task-execution-${var.environment}"

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
        Resource = "arn:aws:secretsmanager:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:secret:AI_SECRETS_${upper(var.environment)}-*"
      }
    ]
  })
}

resource "aws_iam_role" "task_role" {
  name = "pmf-engine-task-${var.environment}"

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
  name = "s3-artifact-access"
  role = aws_iam_role.task_role.id

  # PutObject is permitted on the full bucket because the harness writes its
  # own run artifact at `{experiment_id}/{run_id}/{artifact}.json`. The run_id
  # is generated per task invocation so cross-run collision risk is low.
  #
  # GetObject is scoped to serve-mode prerequisite reads: the only legitimate
  # cross-run read pattern is an experiment (e.g. peer_city_benchmarking)
  # fetching the latest artifact from an upstream experiment (e.g.
  # district_intel). Those are published under `<experiment_id>/latest.json`.
  # Limiting GetObject to `*/latest.json` prevents a compromised runner from
  # exfiltrating arbitrary run artifacts across tenants while still supporting
  # the documented prerequisite pattern.
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "WriteOwnArtifacts"
        Effect = "Allow"
        Action = [
          "s3:PutObject",
          "s3:PutObjectTagging",
        ]
        Resource = "${var.artifact_bucket_arn}/*"
      },
      {
        Sid      = "ReadPrerequisiteArtifacts"
        Effect   = "Allow"
        Action   = "s3:GetObject"
        Resource = "${var.artifact_bucket_arn}/*/latest.json"
      }
    ]
  })
}

resource "aws_iam_role_policy" "task_sqs_callback" {
  name = "sqs-callback-access"
  role = aws_iam_role.task_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "sqs:SendMessage"
        Resource = var.callback_queue_arn
      }
    ]
  })
}

resource "aws_iam_role_policy" "task_cloudwatch_logs" {
  name = "cloudwatch-logs-access"
  role = aws_iam_role.task_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "logs:DescribeLogGroups",
          "logs:DescribeLogStreams",
          "logs:GetLogEvents",
          "logs:FilterLogEvents"
        ]
        Resource = [
          aws_cloudwatch_log_group.runner.arn,
          "${aws_cloudwatch_log_group.runner.arn}:*"
        ]
      }
    ]
  })
}

resource "aws_security_group" "ecs_tasks" {
  name        = "pmf-engine-ecs-tasks-${var.environment}"
  description = "Security group for PMF Engine ECS tasks"
  vpc_id      = var.vpc_id

  egress {
    description = "HTTPS for APIs (Anthropic, S3, SQS)"
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
    Name        = "PMF Engine ECS Tasks"
    Environment = var.environment
  }
}

resource "aws_ecs_cluster" "runner" {
  name = "pmf-engine-${var.environment}"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }

  tags = {
    Environment = var.environment
  }
}

resource "aws_ecs_task_definition" "runner" {
  family                   = "pmf-engine-${var.environment}"
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
      name  = "pmf-engine"
      image = "${var.ecr_repository_url}:${var.docker_image_tag}"

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.runner.name
          "awslogs-region"        = data.aws_region.current.name
          "awslogs-stream-prefix" = "ecs"
        }
      }

      secrets = [
        {
          name      = "ANTHROPIC_API_KEY"
          valueFrom = "arn:aws:secretsmanager:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:secret:AI_SECRETS_${upper(var.environment)}:PMF_ANTHROPIC_API_KEY::"
        },
        {
          name      = "GEMINI_API_KEY"
          valueFrom = "arn:aws:secretsmanager:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:secret:AI_SECRETS_${upper(var.environment)}:GEMINI_API_KEY::"
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
          name      = "DATABRICKS_API_KEY"
          valueFrom = "arn:aws:secretsmanager:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:secret:AI_SECRETS_${upper(var.environment)}:DATABRICKS_API_KEY::"
        },
        {
          name      = "BRAINTRUST_API_KEY"
          valueFrom = "arn:aws:secretsmanager:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:secret:AI_SECRETS_${upper(var.environment)}:BRAINTRUST_API_KEY::"
        }
      ]

      environment = [
        {
          name  = "ENVIRONMENT"
          value = var.environment
        },
        {
          name  = "WORKSPACE_DIR"
          value = "/workspace"
        }
      ]
    }
  ])

  tags = {
    Environment = var.environment
  }
}

resource "aws_sns_topic" "runner_failures" {
  name = "pmf-engine-failures-${var.environment}"

  tags = {
    Name        = "PMF Engine Failures"
    Environment = var.environment
  }
}

resource "aws_sns_topic_subscription" "runner_failures_email" {
  count     = var.failure_notification_email != "" ? 1 : 0
  topic_arn = aws_sns_topic.runner_failures.arn
  protocol  = "email"
  endpoint  = var.failure_notification_email
}

resource "aws_cloudwatch_event_rule" "ecs_task_failed" {
  name        = "pmf-engine-task-failed-${var.environment}"
  description = "Capture ECS task failures for PMF Engine"

  # Match any STOPPED task for our cluster whose failure mode is NOT a clean
  # container exit with code 0. This catches:
  #   (a) container exits with non-zero exit code (task ran but failed)
  #   (b) provisioning failures (CannotPullContainerError, TaskFailedToStart,
  #       ResourceInitializationError) where containers never start and
  #       `exitCode` is absent from the event payload
  # The previous pattern only matched (a) because `anything-but: [0]` on
  # `exitCode` silently drops events where `exitCode` is missing.
  event_pattern = jsonencode({
    source      = ["aws.ecs"]
    detail-type = ["ECS Task State Change"]
    detail = {
      clusterArn = [aws_ecs_cluster.runner.arn]
      lastStatus = ["STOPPED"]
      "$or" = [
        {
          stopCode = [{
            "anything-but" = ["EssentialContainerExited"]
          }]
        },
        {
          containers = {
            exitCode = [{
              "anything-but" = [0]
            }]
          }
        },
        {
          containers = {
            exitCode = [{
              exists = false
            }]
          }
        }
      ]
    }
  })

  tags = {
    Environment = var.environment
  }
}

resource "aws_cloudwatch_event_target" "send_to_sns" {
  rule      = aws_cloudwatch_event_rule.ecs_task_failed.name
  target_id = "SendToSNS"
  arn       = aws_sns_topic.runner_failures.arn

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
  "alarm": "PMF Engine Task Failed",
  "environment": "${var.environment}",
  "cluster": <clusterArn>,
  "taskArn": <taskArn>,
  "stoppedReason": <stoppedReason>,
  "exitCode": <exitCode>,
  "time": <time>,
  "logs": "https://console.aws.amazon.com/cloudwatch/home?region=${data.aws_region.current.name}#logsV2:log-groups/log-group/$252Fecs$252Fpmf-engine-${var.environment}"
}
EOF
  }
}

resource "aws_sns_topic_policy" "runner_failures" {
  arn = aws_sns_topic.runner_failures.arn

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "events.amazonaws.com"
        }
        Action   = "SNS:Publish"
        Resource = aws_sns_topic.runner_failures.arn
      }
    ]
  })
}

resource "aws_sns_topic_subscription" "shared_slack_notifier" {
  count     = var.shared_slack_notifier_lambda_arn != "" ? 1 : 0
  topic_arn = aws_sns_topic.runner_failures.arn
  protocol  = "lambda"
  endpoint  = var.shared_slack_notifier_lambda_arn
}

resource "aws_lambda_permission" "allow_sns_invoke_slack" {
  count         = var.shared_slack_notifier_lambda_arn != "" ? 1 : 0
  statement_id  = "AllowSNSInvokeFromPmfEngineFailures"
  action        = "lambda:InvokeFunction"
  function_name = var.shared_slack_notifier_lambda_arn
  principal     = "sns.amazonaws.com"
  source_arn    = aws_sns_topic.runner_failures.arn
}

output "cluster_name" {
  value       = aws_ecs_cluster.runner.name
  description = "ECS cluster name"
}

output "cluster_arn" {
  value       = aws_ecs_cluster.runner.arn
  description = "ECS cluster ARN"
}

output "task_definition_arn" {
  value       = aws_ecs_task_definition.runner.arn
  description = "ECS task definition ARN"
}

output "task_definition_family" {
  value       = aws_ecs_task_definition.runner.family
  description = "ECS task definition family"
}

output "security_group_id" {
  value       = aws_security_group.ecs_tasks.id
  description = "Security group ID for ECS tasks"
}

output "task_execution_role_arn" {
  value       = aws_iam_role.task_execution_role.arn
  description = "Task execution role ARN"
}

output "task_role_arn" {
  value       = aws_iam_role.task_role.arn
  description = "Task role ARN"
}

output "sns_topic_arn" {
  value       = aws_sns_topic.runner_failures.arn
  description = "SNS topic for runner failure notifications"
}
