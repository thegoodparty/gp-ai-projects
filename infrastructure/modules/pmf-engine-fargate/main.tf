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

variable "broker_security_group_id" {
  description = "Security group ID of the broker service (for egress rules)"
  type        = string
}

variable "broker_url" {
  description = "HTTPS URL of the broker (e.g. https://broker-dev.ai.goodparty.org). Injected into runner env as BROKER_URL + ANTHROPIC_BASE_URL. Must be https — runner's RunnerConfig.from_env() enforces this at boot, but validate at plan-time too to fail fast."
  type        = string

  validation {
    condition     = startswith(lower(var.broker_url), "https://")
    error_message = "broker_url must use https:// — runner rejects plaintext in dev/qa/prod."
  }

  validation {
    condition     = !endswith(var.broker_url, "/")
    error_message = "broker_url must not end with a trailing slash — downstream concatenates paths like ${"/"}anthropic."
  }
}

variable "vpce_security_group_id" {
  description = "Security group ID of the PMF VPC endpoints (ECR, Logs). Runner egress 443 is narrowed to this SG only — image pull + log delivery via endpoints, nothing else on 443. Empty string skips the narrow rule (Phase 1 bring-up before endpoints exist)."
  type        = string
  default     = ""
}

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}
data "aws_vpc" "selected" {
  id = var.vpc_id
}

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

# No custom secrets-manager policy on task execution role — v2 runner has no
# `secrets` block on the task def. The execution role only needs ECR + Logs
# (provided by the attached AmazonECSTaskExecutionRolePolicy managed policy).

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

# Agent task has NO AWS permissions. All operations (S3, SQS, Databricks,
# Anthropic) go through the broker. A compromised agent (via prompt injection)
# cannot exfiltrate data through AWS APIs because the task role has no inline
# policies and no managed policies attached — only the trust policy allowing
# ECS to assume it.

resource "aws_security_group" "ecs_tasks" {
  name        = "pmf-engine-ecs-tasks-${var.environment}"
  description = "Security group for PMF Engine ECS tasks (quarantined: broker-only egress)"
  vpc_id      = var.vpc_id

  tags = {
    Name        = "PMF Engine ECS Tasks"
    Environment = var.environment
  }
}

resource "aws_security_group_rule" "agent_egress_broker" {
  count                    = var.broker_security_group_id != "" ? 1 : 0
  type                     = "egress"
  from_port                = 443
  to_port                  = 443
  protocol                 = "tcp"
  description              = "Broker access only (HTTPS)"
  security_group_id        = aws_security_group.ecs_tasks.id
  source_security_group_id = var.broker_security_group_id
}

resource "aws_security_group_rule" "agent_egress_dns" {
  type              = "egress"
  from_port         = 53
  to_port           = 53
  protocol          = "udp"
  description       = "DNS resolution via VPC DNS"
  security_group_id = aws_security_group.ecs_tasks.id
  cidr_blocks       = [data.aws_vpc.selected.cidr_block]
}

# 443 egress is narrowed to VPC endpoint ENIs only. The ECS platform needs
# this for image pull (ECR) and log streaming (CloudWatch Logs). Runner code
# cannot reach arbitrary internet hosts on 443 because the only valid
# destination is vpce-sg, which hosts AWS-managed endpoint ENIs.
resource "aws_security_group_rule" "agent_egress_vpce" {
  count                    = var.vpce_security_group_id != "" ? 1 : 0
  type                     = "egress"
  from_port                = 443
  to_port                  = 443
  protocol                 = "tcp"
  description              = "ECR + CloudWatch Logs via VPC endpoints"
  security_group_id        = aws_security_group.ecs_tasks.id
  source_security_group_id = var.vpce_security_group_id
}

# S3 gateway endpoint routes traffic via the VPC route table, but the SG on
# the task's ENI still inspects the destination IP — which for S3 is the
# service's public IP range. The AWS-managed prefix list (pl-68a54001 for
# us-west-2 S3) is the narrow allowlist that permits S3 traffic without
# widening to 0.0.0.0/0. Required for ECR image layer blob fetches.
data "aws_prefix_list" "s3" {
  name = "com.amazonaws.us-west-2.s3"
}

resource "aws_security_group_rule" "agent_egress_s3" {
  type              = "egress"
  from_port         = 443
  to_port           = 443
  protocol          = "tcp"
  description       = "S3 via gateway endpoint (for ECR layer blobs)"
  security_group_id = aws_security_group.ecs_tasks.id
  prefix_list_ids   = [data.aws_prefix_list.s3.id]
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

      command = [
        "/bin/bash", "-c",
        "export AWS_EC2_METADATA_DISABLED=true && unset AWS_CONTAINER_CREDENTIALS_RELATIVE_URI && exec python -m pmf_engine.runner.main"
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.runner.name
          "awslogs-region"        = data.aws_region.current.name
          "awslogs-stream-prefix" = "ecs"
        }
      }

      # No secrets injected — runner has zero AWS access in v2. All
      # credentials are held by the broker. If runner code ever needs
      # BRAINTRUST/etc, proxy it through broker.
      environment = [
        {
          name  = "ENVIRONMENT"
          value = var.environment
        },
        {
          name  = "WORKSPACE_DIR"
          value = "/workspace"
        },
        {
          name  = "BROKER_URL"
          value = var.broker_url
        },
        {
          name  = "ANTHROPIC_BASE_URL"
          value = "${var.broker_url}/anthropic"
        },
        {
          name  = "ANTHROPIC_API_KEY"
          value = "placeholder-overridden-by-dispatch"
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
        Sid    = "AllowEventBridgePublish"
        Effect = "Allow"
        Principal = {
          Service = "events.amazonaws.com"
        }
        Action   = "SNS:Publish"
        Resource = aws_sns_topic.runner_failures.arn
      },
      {
        # CloudWatch Alarms publish from cloudwatch.amazonaws.com — needed
        # so the dispatch Lambda's `Errors > 0` alarm (and the SQS DLQ depth
        # alarm) can actually deliver to Slack via shared-slack-notifier.
        # Without this, alarms transition to ALARM but the SNS publish fails
        # silently and Slack never sees the page.
        Sid    = "AllowCloudWatchAlarmPublish"
        Effect = "Allow"
        Principal = {
          Service = "cloudwatch.amazonaws.com"
        }
        Action   = "SNS:Publish"
        Resource = aws_sns_topic.runner_failures.arn
        Condition = {
          StringEquals = {
            "AWS:SourceAccount" = data.aws_caller_identity.current.account_id
          }
        }
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
  statement_id  = "AllowSNSInvokeFromPmfEngineFailures-${var.environment}"
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
