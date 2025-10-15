# Build TypeScript for unified serve-message Lambda
resource "null_resource" "build_serve_message_lambda" {
  triggers = {
    # Rebuild when source files change
    source_hash = filebase64sha256("${var.lambda_source_path}/src/index.ts")
    package_hash = filebase64sha256("${var.lambda_source_path}/package.json")
  }

  provisioner "local-exec" {
    working_dir = var.lambda_source_path
    command = "npm ci && npm run build && cp -r node_modules dist/"
  }
}

# Data source to create zip file for unified serve-message Lambda
data "archive_file" "serve_message_lambda_zip" {
  type        = "zip"
  source_dir  = "${var.lambda_source_path}/dist"
  output_path = "${path.module}/serve_message_lambda.zip"

  depends_on = [null_resource.build_serve_message_lambda]
}

# Unified Serve Message Lambda Function (handles both GET and POST)
resource "aws_lambda_function" "serve_message" {
  filename         = data.archive_file.serve_message_lambda_zip.output_path
  function_name    = "serve-message-${var.environment}"
  role            = var.set_lambda_role_arn
  handler         = "index.handler"
  runtime         = "nodejs22.x"
  timeout         = 30
  memory_size     = 256

  source_code_hash = data.archive_file.serve_message_lambda_zip.output_base64sha256

  environment {
    variables = {
      TABLE_NAME  = var.dynamodb_table_name
      ENVIRONMENT = var.environment
    }
  }

  tags = {
    Name        = "Serve Message Lambda"
    Environment = var.environment
  }
}

# Unified serve message Function URL
resource "aws_lambda_function_url" "serve_message_function_url" {
  function_name      = aws_lambda_function.serve_message.function_name
  authorization_type = "NONE"

  cors {
    allow_credentials = false
    allow_origins     = ["*"]
    allow_methods     = ["GET", "POST"]
    allow_headers     = ["date", "keep-alive", "x-api-key", "content-type", "authorization"]
    expose_headers    = ["date", "keep-alive"]
    max_age          = 86400
  }
}

# CloudWatch Log Group for unified serve message Lambda
resource "aws_cloudwatch_log_group" "serve_message_lambda_logs" {
  name              = "/aws/lambda/serve-message-${var.environment}"
  retention_in_days = 7
}