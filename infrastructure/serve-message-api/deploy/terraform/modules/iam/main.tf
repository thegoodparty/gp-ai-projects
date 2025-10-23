# IAM Role for SET Lambda (Read/Write access)
resource "aws_iam_role" "set_lambda_role" {
  name = "campaign-data-set-lambda-role-${var.environment}"

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
    Name        = "Campaign Data SET Lambda Role"
    Environment = var.environment
  }
}

# IAM Role for RETRIEVE Lambda (Read-only access)
resource "aws_iam_role" "retrieve_lambda_role" {
  name = "campaign-data-retrieve-lambda-role-${var.environment}"

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
    Name        = "Campaign Data RETRIEVE Lambda Role"
    Environment = var.environment
  }
}

# Basic Lambda execution policy for CloudWatch logs
resource "aws_iam_policy" "lambda_basic_execution" {
  name        = "campaign-data-lambda-basic-execution-${var.environment}"
  description = "Basic execution policy for Campaign Data Lambdas"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "arn:aws:logs:*:*:*"
      }
    ]
  })
}

# DynamoDB policy for SET Lambda (Read/Write)
resource "aws_iam_policy" "set_lambda_dynamodb_policy" {
  name        = "campaign-data-set-dynamodb-policy-${var.environment}"
  description = "DynamoDB read/write policy for SET Lambda"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:GetItem",
          "dynamodb:Query"
        ]
        Resource = var.dynamodb_table_arn
      }
    ]
  })
}

# DynamoDB policy for RETRIEVE Lambda (Read-only)
resource "aws_iam_policy" "retrieve_lambda_dynamodb_policy" {
  name        = "campaign-data-retrieve-dynamodb-policy-${var.environment}"
  description = "DynamoDB read-only policy for RETRIEVE Lambda"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:Query"
        ]
        Resource = var.dynamodb_table_arn
      }
    ]
  })
}

# Lambda invoke policy for RETRIEVE Lambda (to forward POST requests to SET Lambda)
resource "aws_iam_policy" "retrieve_lambda_invoke_policy" {
  name        = "campaign-data-retrieve-lambda-invoke-policy-${var.environment}"
  description = "Lambda invoke policy for RETRIEVE Lambda to forward POST requests"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "lambda:InvokeFunction"
        ]
        Resource = "arn:aws:lambda:*:*:function:serve-message-set-${var.environment}"
      }
    ]
  })
}

# Attach policies to SET Lambda role
resource "aws_iam_role_policy_attachment" "set_lambda_basic" {
  role       = aws_iam_role.set_lambda_role.name
  policy_arn = aws_iam_policy.lambda_basic_execution.arn
}

resource "aws_iam_role_policy_attachment" "set_lambda_dynamodb" {
  role       = aws_iam_role.set_lambda_role.name
  policy_arn = aws_iam_policy.set_lambda_dynamodb_policy.arn
}

# Attach policies to RETRIEVE Lambda role
resource "aws_iam_role_policy_attachment" "retrieve_lambda_basic" {
  role       = aws_iam_role.retrieve_lambda_role.name
  policy_arn = aws_iam_policy.lambda_basic_execution.arn
}

resource "aws_iam_role_policy_attachment" "retrieve_lambda_dynamodb" {
  role       = aws_iam_role.retrieve_lambda_role.name
  policy_arn = aws_iam_policy.retrieve_lambda_dynamodb_policy.arn
}

resource "aws_iam_role_policy_attachment" "retrieve_lambda_invoke" {
  role       = aws_iam_role.retrieve_lambda_role.name
  policy_arn = aws_iam_policy.retrieve_lambda_invoke_policy.arn
}

# IAM User for programmatic access to SET Lambda
resource "aws_iam_user" "set_lambda_user" {
  name = "campaign-data-set-user-${var.environment}"

  tags = {
    Name        = "Campaign Data SET User"
    Environment = var.environment
  }
}

# IAM policy for programmatic access to SET Lambda
resource "aws_iam_policy" "set_lambda_invoke_policy" {
  name        = "campaign-data-set-invoke-policy-${var.environment}"
  description = "Policy to invoke SET Lambda function"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "lambda:InvokeFunction"
        ]
        Resource = "arn:aws:lambda:*:*:function:campaign-data-set-${var.environment}"
      }
    ]
  })
}

# Attach invoke policy to SET user
resource "aws_iam_user_policy_attachment" "set_user_invoke" {
  user       = aws_iam_user.set_lambda_user.name
  policy_arn = aws_iam_policy.set_lambda_invoke_policy.arn
}

# Access keys for SET user
resource "aws_iam_access_key" "set_lambda_user_key" {
  user = aws_iam_user.set_lambda_user.name
}

resource "aws_secretsmanager_secret" "set_lambda_credentials" {
  name                    = "serve-message-api/set-lambda-credentials-${var.environment}"
  description             = "IAM Access Keys for Campaign Data SET Lambda user"
  recovery_window_in_days = 7

  tags = {
    Name        = "Campaign Data SET Lambda Credentials"
    Environment = var.environment
  }
}

resource "aws_secretsmanager_secret_version" "set_lambda_credentials" {
  secret_id = aws_secretsmanager_secret.set_lambda_credentials.id
  secret_string = jsonencode({
    access_key_id     = aws_iam_access_key.set_lambda_user_key.id
    secret_access_key = aws_iam_access_key.set_lambda_user_key.secret
    user_name         = aws_iam_user.set_lambda_user.name
    user_arn          = aws_iam_user.set_lambda_user.arn
  })
}