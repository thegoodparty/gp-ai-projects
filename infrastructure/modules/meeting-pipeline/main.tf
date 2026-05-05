data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

locals {
  project = "meeting-pipeline"
}

# ===== CloudWatch Log Groups =====

resource "aws_cloudwatch_log_group" "scan" {
  name              = "/aws/lambda/${local.project}-scan-${var.environment}"
  retention_in_days = 90

  tags = {
    Environment = var.environment
    Project     = local.project
  }
}

resource "aws_cloudwatch_log_group" "process" {
  name              = "/aws/lambda/${local.project}-process-${var.environment}"
  retention_in_days = 90

  tags = {
    Environment = var.environment
    Project     = local.project
  }
}

resource "aws_cloudwatch_log_group" "discover_ecs" {
  name              = "/ecs/${local.project}-discover-${var.environment}"
  retention_in_days = 90

  tags = {
    Environment = var.environment
    Project     = local.project
  }
}

# ===== SQS: Process Queue + DLQ =====

resource "aws_sqs_queue" "process_dlq" {
  name                      = "${local.project}-process-dlq-${var.environment}"
  message_retention_seconds = 1209600 # 14 days

  tags = {
    Environment = var.environment
    Project     = local.project
  }
}

resource "aws_sqs_queue" "process" {
  name = "${local.project}-process-${var.environment}"
  # AWS recommends visibility timeout >= 6× the Lambda timeout (90 min) so
  # in-flight invocations near the 15-min limit don't get duplicated by
  # SQS redelivery. Lambda timeout is 900s.
  visibility_timeout_seconds = 5400
  message_retention_seconds  = 1209600
  receive_wait_time_seconds  = 20

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.process_dlq.arn
    maxReceiveCount     = 3
  })

  tags = {
    Environment = var.environment
    Project     = local.project
  }
}

# ===== SQS: Discover Queue + DLQ =====

resource "aws_sqs_queue" "discover_dlq" {
  name                      = "${local.project}-discover-dlq-${var.environment}"
  message_retention_seconds = 1209600

  tags = {
    Environment = var.environment
    Project     = local.project
  }
}

resource "aws_sqs_queue" "discover" {
  name                       = "${local.project}-discover-${var.environment}"
  visibility_timeout_seconds = 1800 # 30 min (Fargate timeout)
  message_retention_seconds  = 1209600
  receive_wait_time_seconds  = 20

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.discover_dlq.arn
    maxReceiveCount     = 3
  })

  tags = {
    Environment = var.environment
    Project     = local.project
  }
}

# ===== IAM: Shared Lambda Role =====

resource "aws_iam_role" "lambda" {
  name = "${local.project}-lambda-${var.environment}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })

  tags = {
    Environment = var.environment
    Project     = local.project
  }
}

resource "aws_iam_role_policy_attachment" "lambda_basic" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "lambda_permissions" {
  name = "lambda-permissions"
  role = aws_iam_role.lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [
        {
          Effect = "Allow"
          Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:HeadObject"]
          Resource = "arn:aws:s3:::${var.s3_bucket_name}/meeting_pipeline/*"
        },
        {
          Effect    = "Allow"
          Action    = ["s3:ListBucket"]
          Resource  = "arn:aws:s3:::${var.s3_bucket_name}"
          Condition = { StringLike = { "s3:prefix" = "meeting_pipeline/*" } }
        },
        {
          Effect   = "Allow"
          Action   = ["sqs:SendMessage", "sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
          Resource = [aws_sqs_queue.process.arn, aws_sqs_queue.discover.arn]
        },
        {
          Effect   = "Allow"
          Action   = ["secretsmanager:GetSecretValue"]
          Resource = "arn:aws:secretsmanager:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:secret:AI_SECRETS_${upper(var.environment)}-??????"
        },
        {
          Effect   = "Allow"
          Action   = ["sns:Publish"]
          Resource = aws_sns_topic.failures.arn
        },
      ],
      # SendMessage to the QA queue — only emitted when qa_queue_arn is set
      # (queue itself lives in the meeting-qa module). With an empty default,
      # AWS would reject the policy with MalformedPolicyDocument.
      var.qa_queue_arn != "" ? [{
        Effect   = "Allow"
        Action   = ["sqs:SendMessage"]
        Resource = var.qa_queue_arn
      }] : []
    )
  })
}

# ===== Lambda: Scan =====

resource "aws_lambda_function" "scan" {
  function_name = "${local.project}-scan-${var.environment}"
  role          = aws_iam_role.lambda.arn
  package_type  = "Image"
  image_uri     = "${var.ecr_repository_url}:${var.docker_image_tag}"
  architectures = ["arm64"] # CI builds linux/arm64; Lambda default is x86_64
  timeout       = 300
  memory_size   = 512

  image_config {
    command = ["meeting_pipeline.lambda_handlers.scan.handler"]
  }

  environment {
    variables = {
      ENVIRONMENT       = var.environment
      S3_BUCKET         = var.s3_bucket_name
      STORAGE_BACKEND   = "s3"
      SOURCES_PREFIX    = "meeting_pipeline/sources"
      OUTPUT_PREFIX     = "meeting_pipeline/output"
      PROCESS_QUEUE_URL = aws_sqs_queue.process.url
    }
  }

  depends_on = [aws_cloudwatch_log_group.scan]

  tags = {
    Environment = var.environment
    Project     = local.project
  }
}

# ===== Lambda: Process (collect + extract + briefing + QA) =====

resource "aws_lambda_function" "process" {
  function_name = "${local.project}-process-${var.environment}"
  role          = aws_iam_role.lambda.arn
  package_type  = "Image"
  image_uri     = "${var.ecr_repository_url}:${var.docker_image_tag}"
  architectures = ["arm64"] # CI builds linux/arm64; Lambda default is x86_64
  timeout       = 900
  memory_size   = 1024
  # Cap concurrency so SQS-driven fan-out can't blow Gemini per-minute quotas.
  # Tune up if Gemini quota allows; tune down if rate-limit DLQ noise increases.
  reserved_concurrent_executions = 5

  image_config {
    command = ["meeting_pipeline.lambda_handlers.process.handler"]
  }

  environment {
    variables = {
      ENVIRONMENT           = var.environment
      S3_BUCKET             = var.s3_bucket_name
      STORAGE_BACKEND       = "s3"
      SOURCES_PREFIX        = "meeting_pipeline/sources"
      OUTPUT_PREFIX         = "meeting_pipeline/output"
      FAILURE_SNS_TOPIC_ARN = aws_sns_topic.failures.arn
      QA_QUEUE_URL          = var.qa_queue_url
    }
  }

  depends_on = [aws_cloudwatch_log_group.process]

  tags = {
    Environment = var.environment
    Project     = local.project
  }
}

# Process Lambda triggered by SQS
resource "aws_lambda_event_source_mapping" "process_sqs" {
  event_source_arn = aws_sqs_queue.process.arn
  function_name    = aws_lambda_function.process.arn
  batch_size       = 1
  enabled          = true
}

# ===== Step Function: Scan Fan-Out =====

resource "aws_iam_role" "step_function" {
  name = "${local.project}-sfn-${var.environment}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "states.amazonaws.com" }
    }]
  })

  tags = {
    Environment = var.environment
    Project     = local.project
  }
}

resource "aws_iam_role_policy" "step_function_lambda" {
  name = "lambda-invoke"
  role = aws_iam_role.step_function.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["lambda:InvokeFunction"]
      Resource = aws_lambda_function.scan.arn
    }]
  })
}

resource "aws_sfn_state_machine" "scan_fanout" {
  name     = "${local.project}-scan-fanout-${var.environment}"
  role_arn = aws_iam_role.step_function.arn

  definition = jsonencode({
    Comment = "Daily scan: list verified cities, fan out to scan Lambda"
    StartAt = "ListCities"
    States = {
      ListCities = {
        Type     = "Task"
        Resource = aws_lambda_function.scan.arn
        Parameters = { action = "list_cities" }
        ResultPath = "$.result"
        Next       = "ScanAllCities"
        Retry = [{
          ErrorEquals     = ["States.ALL"]
          IntervalSeconds = 30
          MaxAttempts     = 2
          BackoffRate     = 2.0
        }]
        Catch = [{
          ErrorEquals = ["States.ALL"]
          Next        = "Failed"
          ResultPath  = "$.error"
        }]
      }
      ScanAllCities = {
        Type           = "Map"
        ItemsPath      = "$.result.cities"
        MaxConcurrency = 10
        Iterator = {
          StartAt = "ScanOneCity"
          States = {
            ScanOneCity = {
              Type     = "Task"
              Resource = aws_lambda_function.scan.arn
              Parameters = { "slug.$" = "$.slug" }
              Retry = [{
                ErrorEquals     = ["States.ALL"]
                IntervalSeconds = 60
                MaxAttempts     = 2
                BackoffRate     = 2.0
              }]
              End = true
            }
          }
        }
        Next = "Done"
        Catch = [{
          ErrorEquals = ["States.ALL"]
          Next        = "Failed"
          ResultPath  = "$.error"
        }]
      }
      Done   = { Type = "Succeed" }
      Failed = { Type = "Fail", Cause = "Scan fan-out failed", Error = "ScanFanOutError" }
    }
  })

  tags = {
    Environment = var.environment
    Project     = local.project
  }
}

# ===== EventBridge: Daily Cron =====

resource "aws_iam_role" "eventbridge" {
  name = "${local.project}-eventbridge-${var.environment}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "events.amazonaws.com" }
    }]
  })

  tags = {
    Environment = var.environment
    Project     = local.project
  }
}

resource "aws_iam_role_policy" "eventbridge_sfn" {
  name = "step-function-start"
  role = aws_iam_role.eventbridge.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["states:StartExecution"]
      Resource = aws_sfn_state_machine.scan_fanout.arn
    }]
  })
}

resource "aws_cloudwatch_event_rule" "daily_scan" {
  name                = "${local.project}-daily-scan-${var.environment}"
  description         = "Trigger meeting pipeline scan daily at 6 AM UTC"
  schedule_expression = "cron(0 6 * * ? *)"

  tags = {
    Environment = var.environment
    Project     = local.project
  }
}

resource "aws_cloudwatch_event_target" "daily_scan" {
  rule     = aws_cloudwatch_event_rule.daily_scan.name
  arn      = aws_sfn_state_machine.scan_fanout.arn
  role_arn = aws_iam_role.eventbridge.arn
}

# ===== ECS Fargate: Discover =====

resource "aws_ecs_cluster" "discover" {
  name = "${local.project}-discover-${var.environment}"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }

  tags = {
    Environment = var.environment
    Project     = local.project
  }
}

resource "aws_security_group" "discover" {
  name_prefix = "${local.project}-discover-${var.environment}-"
  description = "Discover Fargate task — egress only"
  vpc_id      = var.vpc_id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Environment = var.environment
    Project     = local.project
  }
}

resource "aws_iam_role" "ecs_execution" {
  name = "${local.project}-ecs-execution-${var.environment}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
    }]
  })

  tags = {
    Environment = var.environment
    Project     = local.project
  }
}

resource "aws_iam_role_policy_attachment" "ecs_execution" {
  role       = aws_iam_role.ecs_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "ecs_execution_secrets" {
  name = "secrets-manager"
  role = aws_iam_role.ecs_execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = "arn:aws:secretsmanager:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:secret:AI_SECRETS_${upper(var.environment)}-??????"
    }]
  })
}

resource "aws_iam_role" "ecs_task" {
  name = "${local.project}-ecs-task-${var.environment}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
    }]
  })

  tags = {
    Environment = var.environment
    Project     = local.project
  }
}

resource "aws_iam_role_policy" "ecs_task_permissions" {
  name = "task-permissions"
  role = aws_iam_role.ecs_task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:HeadObject"]
        Resource = "arn:aws:s3:::${var.s3_bucket_name}/meeting_pipeline/*"
      },
      {
        Effect    = "Allow"
        Action    = ["s3:ListBucket"]
        Resource  = "arn:aws:s3:::${var.s3_bucket_name}"
        Condition = { StringLike = { "s3:prefix" = "meeting_pipeline/*" } }
      },
      {
        Effect   = "Allow"
        Action   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
        Resource = aws_sqs_queue.discover.arn
      },
      {
        # discover.py calls inject_secrets() at runtime, which uses boto3 to
        # read AI_SECRETS_<env> via the task role. Without this grant the task
        # crashes on startup.
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = "arn:aws:secretsmanager:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:secret:AI_SECRETS_${upper(var.environment)}-??????"
      },
    ]
  })
}

data "aws_secretsmanager_secret" "ai_secrets" {
  name = "AI_SECRETS_${upper(var.environment)}"
}

resource "aws_ecs_task_definition" "discover" {
  family                   = "${local.project}-discover-${var.environment}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  # Sized for httpx-based discovery (Serper + Firecrawl + Gemini API calls).
  # An earlier plan included Playwright/Chromium which needed 4 vCPU / 16 GB —
  # that plan was scrapped. 0.5 vCPU / 1 GB is plenty for an idle long-poller.
  cpu                = "512"
  memory             = "1024"
  execution_role_arn = aws_iam_role.ecs_execution.arn
  task_role_arn      = aws_iam_role.ecs_task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }

  container_definitions = jsonencode([{
    name  = "discover"
    image = "${var.ecr_repository_url}:${local.project}-discover-${var.environment}"

    environment = [
      { name = "ENVIRONMENT", value = var.environment },
      { name = "S3_BUCKET", value = var.s3_bucket_name },
      { name = "STORAGE_BACKEND", value = "s3" },
      { name = "SOURCES_PREFIX", value = "meeting_pipeline/sources" },
      { name = "DISCOVER_QUEUE_URL", value = aws_sqs_queue.discover.url },
    ]

    secrets = [
      { name = "SERPER_API_KEY", valueFrom = "${data.aws_secretsmanager_secret.ai_secrets.arn}:SERPER_API_KEY::" },
      { name = "FIRECRAWL_API_KEY", valueFrom = "${data.aws_secretsmanager_secret.ai_secrets.arn}:FIRECRAWL_API_KEY::" },
      { name = "TAVILY_API_KEY", valueFrom = "${data.aws_secretsmanager_secret.ai_secrets.arn}:TAVILY_API_KEY::" },
      { name = "GEMINI_API_KEY", valueFrom = "${data.aws_secretsmanager_secret.ai_secrets.arn}:GEMINI_API_KEY::" },
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.discover_ecs.name
        "awslogs-region"        = data.aws_region.current.name
        "awslogs-stream-prefix" = "discover"
      }
    }
  }])

  tags = {
    Environment = var.environment
    Project     = local.project
  }
}

# Always-on Fargate service: keeps one task running to long-poll the discover
# queue. External producers (e.g. an API) just SendMessage to the queue; this
# service picks them up. Force a new deployment via CI when the image changes.
resource "aws_ecs_service" "discover" {
  name            = "${local.project}-discover-${var.environment}"
  cluster         = aws_ecs_cluster.discover.id
  task_definition = aws_ecs_task_definition.discover.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.discover.id]
    assign_public_ip = false # private subnets — VPC NAT must provide internet egress
  }

  # desired_count = 1 with default 100% min healthy would block redeploys.
  # 0% min / 200% max lets ECS stop the old task and start a new one.
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 200

  tags = {
    Environment = var.environment
    Project     = local.project
  }
}

# ===== SNS: Failure Notifications =====

resource "aws_sns_topic" "failures" {
  name = "${local.project}-failures-${var.environment}"

  tags = {
    Environment = var.environment
    Project     = local.project
  }
}

resource "aws_sns_topic_subscription" "failures_email" {
  count     = var.failure_notification_email != "" ? 1 : 0
  topic_arn = aws_sns_topic.failures.arn
  protocol  = "email"
  endpoint  = var.failure_notification_email
}

resource "aws_sns_topic_subscription" "shared_slack_notifier" {
  count     = var.shared_slack_notifier_lambda_arn != "" ? 1 : 0
  topic_arn = aws_sns_topic.failures.arn
  protocol  = "lambda"
  endpoint  = var.shared_slack_notifier_lambda_arn
}

# ===== CloudWatch Alarms: DLQ Depth =====

resource "aws_cloudwatch_metric_alarm" "process_dlq" {
  alarm_name          = "${local.project}-process-dlq-${var.environment}"
  alarm_description   = "Meeting failed to process after 3 retries"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ApproximateNumberOfMessagesVisible"
  namespace           = "AWS/SQS"
  period              = 300
  statistic           = "Maximum"
  threshold           = 0
  treat_missing_data  = "notBreaching"

  dimensions = {
    QueueName = aws_sqs_queue.process_dlq.name
  }

  alarm_actions = [aws_sns_topic.failures.arn]

  tags = {
    Environment = var.environment
    Project     = local.project
  }
}

resource "aws_cloudwatch_metric_alarm" "discover_dlq" {
  alarm_name          = "${local.project}-discover-dlq-${var.environment}"
  alarm_description   = "Discovery failed after 3 retries"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ApproximateNumberOfMessagesVisible"
  namespace           = "AWS/SQS"
  period              = 300
  statistic           = "Maximum"
  threshold           = 0
  treat_missing_data  = "notBreaching"

  dimensions = {
    QueueName = aws_sqs_queue.discover_dlq.name
  }

  alarm_actions = [aws_sns_topic.failures.arn]

  tags = {
    Environment = var.environment
    Project     = local.project
  }
}
