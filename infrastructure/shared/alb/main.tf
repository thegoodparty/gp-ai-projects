# Security Group for ALB
resource "aws_security_group" "alb_sg" {
  name        = "alb-serve-messages-${var.environment}"
  description = "Security group for ALB serving messages API"
  vpc_id      = var.vpc_id

  # Allow HTTP traffic from anywhere
  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
    description = "HTTP traffic from anywhere"
  }

  # Allow HTTPS traffic from anywhere
  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
    description = "HTTPS traffic from anywhere"
  }

  # Allow all outbound traffic
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
    description = "All outbound traffic"
  }

  tags = {
    Name        = "alb-serve-messages-${var.environment}"
    Environment = var.environment
    Purpose     = "ALB Security Group"
  }
}

# Application Load Balancer
resource "aws_lb" "serve_messages_alb" {
  name               = "serve-messages-${var.environment}"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb_sg.id]
  subnets            = var.public_subnet_ids

  enable_deletion_protection = false

  # Access logging (optional)
  access_logs {
    bucket  = aws_s3_bucket.alb_logs.id
    prefix  = "serve-messages-${var.environment}"
    enabled = true
  }

  tags = {
    Name        = "serve-messages-${var.environment}"
    Environment = var.environment
    Purpose     = "Messages API Load Balancer"
  }
}

# S3 bucket for ALB access logs
resource "aws_s3_bucket" "alb_logs" {
  bucket        = "serve-messages-alb-logs-${var.environment}-${random_string.bucket_suffix.result}"
  force_destroy = true

  tags = {
    Name        = "serve-messages-alb-logs-${var.environment}"
    Environment = var.environment
    Purpose     = "ALB Access Logs"
  }
}

resource "random_string" "bucket_suffix" {
  length  = 8
  special = false
  upper   = false
}

# ALB access logs bucket policy
resource "aws_s3_bucket_policy" "alb_logs_policy" {
  bucket = aws_s3_bucket.alb_logs.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AWSLogDeliveryWrite"
        Effect = "Allow"
        Principal = {
          AWS = "arn:aws:iam::797873946194:root" # us-west-2 ELB service account
        }
        Action   = "s3:PutObject"
        Resource = "${aws_s3_bucket.alb_logs.arn}/serve-messages-${var.environment}/AWSLogs/${data.aws_caller_identity.current.account_id}/*"
      },
      {
        Sid    = "AWSLogDeliveryCheck"
        Effect = "Allow"
        Principal = {
          AWS = "arn:aws:iam::797873946194:root" # us-west-2 ELB service account
        }
        Action   = "s3:GetBucketAcl"
        Resource = aws_s3_bucket.alb_logs.arn
      }
    ]
  })
}

data "aws_caller_identity" "current" {}

# Target Group for Lambda Function
resource "aws_lb_target_group" "serve_message" {
  name        = "serve-message-${var.environment}"
  target_type = "lambda"

  tags = {
    Name        = "serve-message-${var.environment}"
    Environment = var.environment
    Purpose     = "Lambda Target Group"
  }
}

# Target Group Attachment for Lambda
resource "aws_lb_target_group_attachment" "serve_message" {
  target_group_arn = aws_lb_target_group.serve_message.arn
  target_id        = var.serve_message_lambda_arn
  depends_on       = [aws_lambda_permission.alb_invoke]
}

# Lambda permission for ALB to invoke function
resource "aws_lambda_permission" "alb_invoke" {
  statement_id  = "AllowExecutionFromALB"
  action        = "lambda:InvokeFunction"
  function_name = var.serve_message_lambda_function_name
  principal     = "elasticloadbalancing.amazonaws.com"
  source_arn    = aws_lb_target_group.serve_message.arn
}

# HTTPS Listener
resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.serve_messages_alb.arn
  port              = "443"
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS-1-2-2017-01"
  certificate_arn   = var.certificate_arn

  # Default action - return 404 for unmatched routes
  default_action {
    type = "fixed-response"

    fixed_response {
      content_type = "application/json"
      message_body = jsonencode({
        error = "Not Found"
        message = "The requested path was not found"
      })
      status_code = "404"
    }
  }

  tags = {
    Name        = "https-listener-${var.environment}"
    Environment = var.environment
  }
}

# HTTP Listener - Redirect to HTTPS
resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.serve_messages_alb.arn
  port              = "80"
  protocol          = "HTTP"

  default_action {
    type = "redirect"

    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }

  tags = {
    Name        = "http-listener-${var.environment}"
    Environment = var.environment
  }
}

# Listener Rule for /serve/messages/* with valid API key
resource "aws_lb_listener_rule" "serve_messages_valid" {
  listener_arn = aws_lb_listener.https.arn
  priority     = 20

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.serve_message.arn
  }

  condition {
    path_pattern {
      values = ["/serve/messages/*"]
    }
  }

  condition {
    http_header {
      http_header_name = "x-api-key"
      values          = [var.api_key]
    }
  }

  tags = {
    Name        = "serve-messages-valid-${var.environment}"
    Environment = var.environment
  }
}

# Listener Rule for /serve/messages/* without valid API key - return 403
resource "aws_lb_listener_rule" "serve_messages_invalid" {
  listener_arn = aws_lb_listener.https.arn
  priority     = 30

  action {
    type = "fixed-response"

    fixed_response {
      content_type = "application/json"
      message_body = jsonencode({
        error = "Forbidden"
        message = "Invalid or missing API key"
      })
      status_code = "403"
    }
  }

  condition {
    path_pattern {
      values = ["/serve/messages/*"]
    }
  }

  tags = {
    Name        = "serve-messages-invalid-${var.environment}"
    Environment = var.environment
  }
}

