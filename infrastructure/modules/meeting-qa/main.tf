data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

locals {
  project   = "meeting-qa"
  image_tag = var.docker_image_tag != "" ? var.docker_image_tag : "${local.project}-${var.environment}"
}

# ===== CloudWatch Log Group =====

resource "aws_cloudwatch_log_group" "qa" {
  name              = "/aws/lambda/${local.project}-${var.environment}"
  retention_in_days = var.log_retention_days

  tags = {
    Environment = var.environment
    Project     = local.project
  }
}

# ===== SQS: QA Queue + DLQ =====

resource "aws_sqs_queue" "qa_dlq" {
  name                      = "${local.project}-dlq-${var.environment}"
  message_retention_seconds = 1209600 # 14 days

  tags = {
    Environment = var.environment
    Project     = local.project
  }
}

resource "aws_sqs_queue" "qa" {
  name                       = "${local.project}-${var.environment}"
  visibility_timeout_seconds = var.lambda_timeout_seconds
  message_retention_seconds  = 1209600
  receive_wait_time_seconds  = 20

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.qa_dlq.arn
    maxReceiveCount     = 3
  })

  tags = {
    Environment = var.environment
    Project     = local.project
  }
}

# ===== IAM: QA Lambda Role =====

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
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:HeadObject"]
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
        Resource = aws_sqs_queue.qa.arn
      },
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = "arn:aws:secretsmanager:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:secret:AI_SECRETS_${upper(var.environment)}-??????"
      },
    ]
  })
}

# ===== Lambda: QA =====

resource "aws_lambda_function" "qa" {
  function_name = "${local.project}-${var.environment}"
  role          = aws_iam_role.lambda.arn
  package_type  = "Image"
  image_uri     = "${var.ecr_repository_url}:${local.image_tag}"
  timeout       = var.lambda_timeout_seconds
  memory_size   = var.lambda_memory_mb

  image_config {
    command = ["meeting_qa.lambda_handler.handler"]
  }

  environment {
    variables = {
      ENVIRONMENT     = var.environment
      S3_BUCKET       = var.s3_bucket_name
      STORAGE_BACKEND = "s3"
    }
  }

  depends_on = [aws_cloudwatch_log_group.qa]

  tags = {
    Environment = var.environment
    Project     = local.project
  }
}

resource "aws_lambda_event_source_mapping" "qa_sqs" {
  event_source_arn = aws_sqs_queue.qa.arn
  function_name    = aws_lambda_function.qa.arn
  batch_size       = 1
  enabled          = true
}
