variable "environment" {
  description = "Environment name (dev, prod)"
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
  description = "Docker image tag for serve-analyze (default: dev)"
  type        = string
  default     = "dev"
}

variable "dynamodb_table_name" {
  description = "DynamoDB table name for storing results"
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

resource "aws_s3_bucket" "pipeline_data" {
  bucket = "serve-analyze-data-${var.environment}"

  tags = {
    Name        = "V1 Pipeline Data"
    Environment = var.environment
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "pipeline_data_lifecycle" {
  bucket = aws_s3_bucket.pipeline_data.id

  rule {
    id     = "cleanup-old-data"
    status = "Enabled"

    expiration {
      days = 30
    }

    filter {
      prefix = "input/"
    }
  }

  rule {
    id     = "archive-outputs"
    status = "Enabled"

    transition {
      days          = 7
      storage_class = "GLACIER"
    }

    filter {
      prefix = "output/"
    }
  }
}

resource "aws_cloudwatch_log_group" "pipeline" {
  name              = "/ecs/serve-analyze-${var.environment}"
  retention_in_days = 30

  tags = {
    Environment = var.environment
  }
}

resource "aws_iam_role" "task_execution_role" {
  name = "serve-analyze-task-execution-${var.environment}"

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

# Secrets Manager access for task execution role
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
  name = "serve-analyze-task-${var.environment}"

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
          "s3:GetObject",
          "s3:ListBucket"
        ]
        Resource = [
          aws_s3_bucket.pipeline_data.arn,
          "${aws_s3_bucket.pipeline_data.arn}/input/*"
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "s3:PutObject"
        ]
        Resource = [
          "${aws_s3_bucket.pipeline_data.arn}/output/*"
        ]
      }
    ]
  })
}

resource "aws_iam_role_policy" "task_dynamodb_access" {
  name = "dynamodb-access"
  role = aws_iam_role.task_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:BatchWriteItem"
        ]
        Resource = "arn:aws:dynamodb:*:*:table/${var.dynamodb_table_name}"
      }
    ]
  })
}

resource "aws_security_group" "ecs_tasks" {
  name        = "serve-analyze-ecs-tasks-${var.environment}"
  description = "Security group for V1 Pipeline ECS tasks"
  vpc_id      = var.vpc_id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name        = "V1 Pipeline ECS Tasks"
    Environment = var.environment
  }
}

resource "aws_ecs_task_definition" "pipeline" {
  family                   = "serve-analyze-${var.environment}"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "4096"
  memory                   = "16384"
  execution_role_arn       = aws_iam_role.task_execution_role.arn
  task_role_arn            = aws_iam_role.task_role.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }

  container_definitions = jsonencode([
    {
      name  = "serve-analyze"
      image = "${var.ecr_repository_url}:${var.docker_image_tag}"

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.pipeline.name
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
          name      = "SERVE_API_KEY"
          valueFrom = "arn:aws:secretsmanager:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:secret:AI_SECRETS_${upper(var.environment)}:SERVE_API_KEY::"
        },
        {
          name      = "SLACK_WEBHOOK_URL"
          valueFrom = "arn:aws:secretsmanager:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:secret:AI_SECRETS_${upper(var.environment)}:SLACK_WEBHOOK_URL::"
        }
      ]

      environment = [
        {
          name  = "API_URL"
          value = var.environment == "prod" ? "https://ai.goodparty.org/serve/messages" : "https://ai-${var.environment}.goodparty.org/serve/messages"
        },
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

resource "aws_iam_role" "lambda_trigger" {
  name = "serve-analyze-lambda-trigger-${var.environment}"

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

resource "aws_iam_role_policy" "lambda_step_functions_trigger" {
  name = "step-functions-trigger"
  role = aws_iam_role.lambda_trigger.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "states:StartExecution"
        ]
        Resource = aws_sfn_state_machine.pipeline.arn
      }
    ]
  })
}

resource "aws_lambda_function" "pipeline_trigger" {
  filename      = "lambda-trigger.zip"
  function_name = "serve-analyze-trigger-${var.environment}"
  role          = aws_iam_role.lambda_trigger.arn
  handler       = "index.handler"
  runtime       = "nodejs22.x"
  timeout       = 60

  environment {
    variables = {
      STATE_MACHINE_ARN    = aws_sfn_state_machine.pipeline.arn
      ECS_CLUSTER_NAME     = aws_ecs_cluster.pipeline.name
      TASK_DEFINITION_ARN  = aws_ecs_task_definition.pipeline.arn
      SUBNET_IDS           = join(",", var.private_subnet_ids)
      SECURITY_GROUP_ID    = aws_security_group.ecs_tasks.id
      S3_OUTPUT_BUCKET     = aws_s3_bucket.pipeline_data.id
      SNS_TOPIC_ARN        = aws_sns_topic.pipeline_failures.arn
    }
  }

  tags = {
    Environment = var.environment
  }
}

resource "aws_lambda_permission" "allow_s3" {
  statement_id  = "AllowExecutionFromS3"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.pipeline_trigger.function_name
  principal     = "s3.amazonaws.com"
  source_arn    = aws_s3_bucket.pipeline_data.arn
}

resource "aws_s3_bucket_notification" "pipeline_trigger" {
  bucket = aws_s3_bucket.pipeline_data.id

  lambda_function {
    lambda_function_arn = aws_lambda_function.pipeline_trigger.arn
    events              = ["s3:ObjectCreated:*"]
    filter_prefix       = "input/"
    filter_suffix       = ".csv"
  }

  depends_on = [aws_lambda_permission.allow_s3]
}

resource "aws_ecs_cluster" "pipeline" {
  name = "serve-analyze-${var.environment}"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }

  tags = {
    Environment = var.environment
  }
}

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

output "cluster_name" {
  value = aws_ecs_cluster.pipeline.name
}

output "task_definition_arn" {
  value = aws_ecs_task_definition.pipeline.arn
}

output "lambda_function_arn" {
  value = aws_lambda_function.pipeline_trigger.arn
}

output "lambda_function_name" {
  value = aws_lambda_function.pipeline_trigger.function_name
}

output "s3_bucket_name" {
  value = aws_s3_bucket.pipeline_data.id
}

output "security_group_id" {
  value = aws_security_group.ecs_tasks.id
}

resource "aws_sns_topic" "pipeline_failures" {
  name = "serve-analyze-pipeline-failures-${var.environment}"

  tags = {
    Name        = "Pipeline Failures"
    Environment = var.environment
  }
}

resource "aws_sns_topic_subscription" "pipeline_failures_email" {
  count     = var.failure_notification_email != "" ? 1 : 0
  topic_arn = aws_sns_topic.pipeline_failures.arn
  protocol  = "email"
  endpoint  = var.failure_notification_email
}

resource "aws_cloudwatch_event_rule" "ecs_task_failed" {
  name        = "serve-analyze-task-failed-${var.environment}"
  description = "Capture ECS task failures for serve-analyze pipeline"

  event_pattern = jsonencode({
    source      = ["aws.ecs"]
    detail-type = ["ECS Task State Change"]
    detail = {
      clusterArn  = [aws_ecs_cluster.pipeline.arn]
      lastStatus  = ["STOPPED"]
      stoppedReason = [{
        "anything-but" = { "prefix" = "Essential container" }
      }]
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
  arn       = aws_sns_topic.pipeline_failures.arn

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
  "alarm": "ECS Task Failed",
  "environment": "${var.environment}",
  "cluster": <clusterArn>,
  "taskArn": <taskArn>,
  "stoppedReason": <stoppedReason>,
  "exitCode": <exitCode>,
  "time": <time>,
  "logs": "https://console.aws.amazon.com/cloudwatch/home?region=${data.aws_region.current.name}#logsV2:log-groups/log-group/$252Fecs$252Fserve-analyze-${var.environment}"
}
EOF
  }
}

resource "aws_sns_topic_policy" "pipeline_failures" {
  arn = aws_sns_topic.pipeline_failures.arn

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "events.amazonaws.com"
        }
        Action   = "SNS:Publish"
        Resource = aws_sns_topic.pipeline_failures.arn
      }
    ]
  })
}

resource "aws_iam_role" "step_functions" {
  name = "serve-analyze-step-functions-${var.environment}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "states.amazonaws.com"
        }
      }
    ]
  })
}

resource "aws_iam_role_policy" "step_functions_ecs" {
  name = "ecs-execution"
  role = aws_iam_role.step_functions.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "ecs:RunTask",
          "ecs:StopTask",
          "ecs:DescribeTasks",
          "ecs:TagResource"
        ]
        Resource = "*"
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
      },
      {
        Effect = "Allow"
        Action = [
          "events:PutTargets",
          "events:PutRule",
          "events:DescribeRule"
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "sns:Publish"
        ]
        Resource = aws_sns_topic.pipeline_failures.arn
      }
    ]
  })
}

resource "aws_sfn_state_machine" "pipeline" {
  name     = "serve-analyze-pipeline-${var.environment}"
  role_arn = aws_iam_role.step_functions.arn

  definition = templatefile("${path.module}/step-function-definition.json", {
    sns_topic_arn = aws_sns_topic.pipeline_failures.arn
  })

  tags = {
    Environment = var.environment
  }
}

output "sns_topic_arn" {
  value       = aws_sns_topic.pipeline_failures.arn
  description = "SNS topic for pipeline failure notifications"
}

output "state_machine_arn" {
  value       = aws_sfn_state_machine.pipeline.arn
  description = "Step Functions state machine ARN"
}

# Subscribe to shared Slack notifier Lambda if provided
resource "aws_sns_topic_subscription" "shared_slack_notifier" {
  count     = var.shared_slack_notifier_lambda_arn != "" ? 1 : 0
  topic_arn = aws_sns_topic.pipeline_failures.arn
  protocol  = "lambda"
  endpoint  = var.shared_slack_notifier_lambda_arn
}
