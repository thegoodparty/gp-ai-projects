# Security Group for ALB
resource "aws_security_group" "alb_sg" {
  name        = "alb-ai-${var.environment}"
  description = "Security group for ALB serving AI API"
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
    Name        = "alb-ai-${var.environment}"
    Environment = var.environment
    Purpose     = "ALB Security Group"
  }
}

# Application Load Balancer
resource "aws_lb" "serve_messages_alb" {
  name               = "ai-${var.environment}"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb_sg.id]
  subnets            = var.public_subnet_ids

  enable_deletion_protection = false

  # Access logging (optional)
  access_logs {
    bucket  = aws_s3_bucket.alb_logs.id
    prefix  = "ai-${var.environment}"
    enabled = true
  }

  tags = {
    Name        = "ai-${var.environment}"
    Environment = var.environment
    Purpose     = "AI API Load Balancer"
  }
}

# S3 bucket for ALB access logs
resource "aws_s3_bucket" "alb_logs" {
  bucket        = "ai-alb-logs-${var.environment}-${random_string.bucket_suffix.result}"
  force_destroy = true

  tags = {
    Name        = "ai-alb-logs-${var.environment}"
    Environment = var.environment
    Purpose     = "ALB Access Logs"
  }
}

resource "random_string" "bucket_suffix" {
  length  = 8
  special = false
  upper   = false
}

# S3 bucket encryption for ALB logs
resource "aws_s3_bucket_server_side_encryption_configuration" "alb_logs" {
  bucket = aws_s3_bucket.alb_logs.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# S3 bucket public access block for ALB logs
resource "aws_s3_bucket_public_access_block" "alb_logs" {
  bucket = aws_s3_bucket.alb_logs.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
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
        Resource = "${aws_s3_bucket.alb_logs.arn}/ai-${var.environment}/AWSLogs/${data.aws_caller_identity.current.account_id}/*"
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
