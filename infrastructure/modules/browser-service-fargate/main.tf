variable "environment" {
  description = "Environment name (dev, qa, prod)"
  type        = string

  validation {
    condition     = contains(["dev", "qa", "prod"], var.environment)
    error_message = "Environment must be one of: dev, qa, prod."
  }
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
  description = "Docker image tag for browser-service"
  type        = string
  default     = "browser-service-dev"
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
  default     = "2048"
}

variable "task_memory" {
  description = "Memory for ECS Fargate task in MB"
  type        = string
  default     = "4096"
}

variable "vpc_cidr_block" {
  description = "VPC CIDR block for security group ingress"
  type        = string
}

variable "desired_count" {
  description = "Number of ECS service tasks to run"
  type        = number
  default     = 1
}

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

# ------------------------------------------------------------------------------
# CloudWatch Log Group
# ------------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "service" {
  name              = "/ecs/browser-service-${var.environment}"
  retention_in_days = 30

  tags = {
    Environment = var.environment
  }
}

# ------------------------------------------------------------------------------
# IAM Task Execution Role (pulls images, pushes logs)
# ------------------------------------------------------------------------------

resource "aws_iam_role" "task_execution_role" {
  name = "browser-service-task-execution-${var.environment}"

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

# ------------------------------------------------------------------------------
# IAM Task Role (runtime permissions)
# ------------------------------------------------------------------------------

resource "aws_iam_role" "task_role" {
  name = "browser-service-task-${var.environment}"

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
        Resource = "${aws_cloudwatch_log_group.service.arn}:*"
      }
    ]
  })
}

# ------------------------------------------------------------------------------
# Security Group
# ------------------------------------------------------------------------------

resource "aws_security_group" "ecs_tasks" {
  name        = "browser-service-ecs-${var.environment}"
  description = "Security group for Browser Service ECS tasks"
  vpc_id      = var.vpc_id

  ingress {
    description = "HTTP API from VPC"
    from_port   = 8000
    to_port     = 8000
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr_block]
  }

  egress {
    description = "HTTPS for fetching web pages"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "HTTP for fetching web pages"
    from_port   = 80
    to_port     = 80
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
    Name        = "Browser Service ECS Tasks"
    Environment = var.environment
  }
}

# ------------------------------------------------------------------------------
# ECS Cluster
# ------------------------------------------------------------------------------

resource "aws_ecs_cluster" "service" {
  name = "browser-service-${var.environment}"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }

  tags = {
    Environment = var.environment
  }
}

# ------------------------------------------------------------------------------
# Cloud Map Service Discovery
# ------------------------------------------------------------------------------

resource "aws_service_discovery_private_dns_namespace" "service" {
  name        = "browser-service.internal"
  vpc         = var.vpc_id
  description = "Private DNS namespace for browser service discovery"

  tags = {
    Environment = var.environment
  }
}

resource "aws_service_discovery_service" "service" {
  name = "browser-service"

  dns_config {
    namespace_id = aws_service_discovery_private_dns_namespace.service.id

    dns_records {
      ttl  = 10
      type = "A"
    }

    routing_policy = "MULTIVALUE"
  }

  health_check_custom_config {
    failure_threshold = 1
  }

  tags = {
    Environment = var.environment
  }
}

# ------------------------------------------------------------------------------
# ECS Task Definition
# ------------------------------------------------------------------------------

resource "aws_ecs_task_definition" "service" {
  family                   = "browser-service-${var.environment}"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.task_execution_role.arn
  task_role_arn            = aws_iam_role.task_role.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([
    {
      name  = "browser-service"
      image = "${var.ecr_repository_url}:${var.docker_image_tag}"

      portMappings = [
        {
          containerPort = 8000
          protocol      = "tcp"
        }
      ]

      healthCheck = {
        command     = ["CMD-SHELL", "curl -f http://localhost:8000/health || exit 1"]
        interval    = 30
        timeout     = 5
        retries     = 3
        startPeriod = 60
      }

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.service.name
          "awslogs-region"        = data.aws_region.current.name
          "awslogs-stream-prefix" = "ecs"
        }
      }

      environment = [
        {
          name  = "ENVIRONMENT"
          value = var.environment
        }
      ]
    }
  ])

  tags = {
    Environment = var.environment
  }
}

# ------------------------------------------------------------------------------
# ECS Service (always-running)
# ------------------------------------------------------------------------------

resource "aws_ecs_service" "service" {
  name            = "browser-service-${var.environment}"
  cluster         = aws_ecs_cluster.service.id
  task_definition = aws_ecs_task_definition.service.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.ecs_tasks.id]
    assign_public_ip = false
  }

  service_registries {
    registry_arn = aws_service_discovery_service.service.arn
  }

  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  health_check_grace_period_seconds  = 120
  propagate_tags                     = "TASK_DEFINITION"
  enable_execute_command             = true

  tags = {
    Environment = var.environment
  }
}

# ------------------------------------------------------------------------------
# SNS + EventBridge for failure notifications
# ------------------------------------------------------------------------------

resource "aws_sns_topic" "service_failures" {
  name = "browser-service-failures-${var.environment}"

  tags = {
    Name        = "Browser Service Failures"
    Environment = var.environment
  }
}

resource "aws_sns_topic_subscription" "service_failures_email" {
  count     = var.failure_notification_email != "" ? 1 : 0
  topic_arn = aws_sns_topic.service_failures.arn
  protocol  = "email"
  endpoint  = var.failure_notification_email
}

resource "aws_cloudwatch_event_rule" "ecs_task_failed" {
  name        = "browser-service-task-failed-${var.environment}"
  description = "Capture ECS task failures for Browser Service"

  event_pattern = jsonencode({
    source      = ["aws.ecs"]
    detail-type = ["ECS Task State Change"]
    detail = {
      clusterArn = [aws_ecs_cluster.service.arn]
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
  arn       = aws_sns_topic.service_failures.arn

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
  "alarm": "Browser Service Task Failed",
  "environment": "${var.environment}",
  "cluster": <clusterArn>,
  "taskArn": <taskArn>,
  "stoppedReason": <stoppedReason>,
  "exitCode": <exitCode>,
  "time": <time>,
  "logs": "https://console.aws.amazon.com/cloudwatch/home?region=${data.aws_region.current.name}#logsV2:log-groups/log-group/$252Fecs$252Fbrowser-service-${var.environment}"
}
EOF
  }
}

resource "aws_sns_topic_policy" "service_failures" {
  arn = aws_sns_topic.service_failures.arn

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "events.amazonaws.com"
        }
        Action   = "SNS:Publish"
        Resource = aws_sns_topic.service_failures.arn
      }
    ]
  })
}

resource "aws_sns_topic_subscription" "shared_slack_notifier" {
  count     = var.shared_slack_notifier_lambda_arn != "" ? 1 : 0
  topic_arn = aws_sns_topic.service_failures.arn
  protocol  = "lambda"
  endpoint  = var.shared_slack_notifier_lambda_arn
}

resource "aws_lambda_permission" "allow_sns_invoke_slack" {
  count         = var.shared_slack_notifier_lambda_arn != "" ? 1 : 0
  statement_id  = "AllowSNSInvokeFromServiceFailures"
  action        = "lambda:InvokeFunction"
  function_name = var.shared_slack_notifier_lambda_arn
  principal     = "sns.amazonaws.com"
  source_arn    = aws_sns_topic.service_failures.arn
}

# ------------------------------------------------------------------------------
# Outputs
# ------------------------------------------------------------------------------

output "cluster_name" {
  value       = aws_ecs_cluster.service.name
  description = "ECS cluster name"
}

output "cluster_arn" {
  value       = aws_ecs_cluster.service.arn
  description = "ECS cluster ARN"
}

output "task_definition_arn" {
  value       = aws_ecs_task_definition.service.arn
  description = "ECS task definition ARN"
}

output "service_name" {
  value       = aws_ecs_service.service.name
  description = "ECS service name"
}

output "service_arn" {
  value       = aws_ecs_service.service.id
  description = "ECS service ARN"
}

output "security_group_id" {
  value       = aws_security_group.ecs_tasks.id
  description = "Security group ID for ECS tasks"
}

output "namespace_id" {
  value       = aws_service_discovery_private_dns_namespace.service.id
  description = "Cloud Map namespace ID"
}

output "discovery_service_arn" {
  value       = aws_service_discovery_service.service.arn
  description = "Cloud Map service discovery ARN"
}
