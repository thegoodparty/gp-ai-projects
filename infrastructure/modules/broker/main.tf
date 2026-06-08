variable "environment" {
  description = "Environment name (dev, qa, prod)"
  type        = string

  validation {
    condition     = contains(["dev", "qa", "prod"], var.environment)
    error_message = "environment must be one of: dev, qa, prod."
  }
}

variable "vpc_id" {
  description = "VPC ID for ECS tasks"
  type        = string
}

variable "private_subnet_ids" {
  description = "Private subnet IDs for ECS service"
  type        = list(string)
}

variable "ecr_repository_url" {
  description = "ECR repository URL for broker Docker image"
  type        = string
}

variable "docker_image_tag" {
  description = "Docker image tag for the broker"
  type        = string
  default     = "broker-dev"
}

variable "agent_security_group_id" {
  description = "Security group ID of agent tasks (for ingress rules)"
  type        = string
}

variable "dispatch_lambda_security_group_id" {
  description = "Security group ID of dispatch Lambda (for ingress rules). Empty string disables the rule."
  type        = string
  default     = ""
}

variable "artifact_bucket_arn" {
  description = "ARN of the S3 artifacts bucket"
  type        = string
}

variable "artifact_bucket_name" {
  description = "Name of the S3 artifacts bucket"
  type        = string
}

variable "experiment_metadata_bucket_name" {
  description = "Name of the S3 metadata bucket holding PMF experiment manifests + instructions. Injected as EXPERIMENT_METADATA_BUCKET env var. Set this once the agent-experiment-metadata module is provisioned for the env."
  type        = string
  default     = ""
}

variable "experiment_metadata_read_policy_arn" {
  description = "ARN of the managed IAM policy granting read access to the experiment metadata bucket (output by the agent-experiment-metadata module). Empty string skips the attachment (Phase 1 bring-up where the bucket isn't provisioned yet)."
  type        = string
  default     = ""
}

variable "sns_topic_arn" {
  description = "ARN of the SNS topic for alarm notifications. Empty string disables alarms."
  type        = string
  default     = ""
}

variable "gp_api_sqs_queue_arn" {
  description = "ARN of the gp-api results queue (for reference only; broker sends to its own results queue)"
  type        = string
}

variable "results_queue_arn" {
  description = "ARN of the external SQS FIFO queue the broker sends results to (owned by the control-plane stack)"
  type        = string
}

variable "results_queue_url" {
  description = "URL of the external SQS FIFO queue the broker sends results to (injected into broker task env as RESULTS_QUEUE_URL)"
  type        = string
}

variable "public_zone_id" {
  description = "Route53 public hosted zone ID that hosts broker DNS records and ACM validation CNAMEs. Defaults to goodparty.org."
  type        = string
  default     = "Z10392302OXMPNQLPO07K"
}

variable "hostname" {
  description = "Fully-qualified broker hostname (ACM cert SAN + Route53 record). Example: broker-dev.ai.goodparty.org. Leave empty to use broker-{env}.ai.goodparty.org for non-prod or broker.ai.goodparty.org for prod."
  type        = string
  default     = ""
}

variable "agent_run_inputs_read_policy_arn" {
  description = "ARN of the managed IAM policy granting GetObject on the agent-run-inputs bucket (sourced from the agent-run-inputs Terraform stack's read_policy_arn output). Attached to the broker task role so /inputs/read can fetch user-uploaded files on the runner's behalf. Empty string skips the attachment entirely (local-dev / pre-bucket bootstrap)."
  type        = string
  default     = ""
}

locals {
  broker_hostname = var.hostname != "" ? var.hostname : (
    var.environment == "prod" ? "broker.ai.goodparty.org" : "broker-${var.environment}.ai.goodparty.org"
  )
  broker_url = "https://${local.broker_hostname}"
}

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}
data "aws_vpc" "selected" {
  id = var.vpc_id
}

# --- CloudWatch Log Group ---

resource "aws_cloudwatch_log_group" "broker" {
  name              = "/ecs/broker-${var.environment}"
  retention_in_days = 30

  tags = {
    Environment = var.environment
  }
}

# --- Secrets Manager ---

resource "aws_secretsmanager_secret" "broker" {
  name        = "broker-${var.environment}"
  description = "Secrets for PMF broker service. Operator populates: ANTHROPIC_API_KEY, GEMINI_API_KEY, TAVILY_API_KEY, DATABRICKS_SERVER_HOSTNAME, DATABRICKS_HTTP_PATH, DATABRICKS_API_KEY, SERVICE_TOKEN_HASH, CLERK_SECRET_KEY, CLERK_FRONTEND_API_BASE, GP_API_BASE_URL, AGENT_FLEET_CLERK_ID, AGENT_MCP_TOKEN_SECRET, BRAINTRUST_API_KEY"

  tags = {
    Environment = var.environment
  }
}

resource "aws_secretsmanager_secret_version" "broker_initial" {
  secret_id     = aws_secretsmanager_secret.broker.id
  secret_string = jsonencode({})

  lifecycle {
    ignore_changes = [secret_string]
  }
}

# --- DynamoDB: Scope Tickets ---

resource "aws_dynamodb_table" "scope_tickets" {
  name         = "broker-scope-tickets-${var.environment}"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"

  attribute {
    name = "pk"
    type = "S"
  }

  ttl {
    attribute_name = "exp"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = {
    Environment = var.environment
  }
}

# --- SQS: Results Queue ---
# The results queue itself is owned by the control-plane stack (agent-results-{env}.fifo).
# Broker's access is granted via the broker task role's IAM policy (task_sqs_results below).
# No aws_sqs_queue_policy here — IAM role policy is sufficient and avoids conflicting with
# any resource policy the control-plane owner may add later.

# --- IAM: Task Execution Role ---

resource "aws_iam_role" "task_execution_role" {
  name = "broker-task-execution-${var.environment}"

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
        Resource = aws_secretsmanager_secret.broker.arn
      }
    ]
  })
}

# --- IAM: Task Role ---

resource "aws_iam_role" "task_role" {
  name = "broker-task-${var.environment}"

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

resource "aws_iam_role_policy" "task_dynamodb" {
  name = "dynamodb-scope-tickets"
  role = aws_iam_role.task_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:DeleteItem",
          "dynamodb:Query"
        ]
        Resource = aws_dynamodb_table.scope_tickets.arn
      }
    ]
  })
}

resource "aws_iam_role_policy" "task_s3" {
  name = "s3-artifact-access"
  role = aws_iam_role.task_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "s3:PutObject",
          "s3:GetObject"
        ]
        Resource = "${var.artifact_bucket_arn}/*"
      }
    ]
  })
}

# Read-only access to the agent-run-inputs bucket (one per environment). The
# managed policy is owned by the agent-run-inputs Terraform stack; we attach
# it here so the broker task role gains GetObject on the bucket. /inputs/read
# enforces per-request authorization against the ScopeTicket's enumerated
# input_files list — this attachment is just the underlying AWS grant.
resource "aws_iam_role_policy_attachment" "task_agent_run_inputs_read" {
  count      = var.agent_run_inputs_read_policy_arn != "" ? 1 : 0
  role       = aws_iam_role.task_role.name
  policy_arn = var.agent_run_inputs_read_policy_arn
}

resource "aws_iam_role_policy_attachment" "task_experiment_metadata_read" {
  count      = var.experiment_metadata_read_policy_arn != "" ? 1 : 0
  role       = aws_iam_role.task_role.name
  policy_arn = var.experiment_metadata_read_policy_arn
}

resource "aws_iam_role_policy" "task_sqs_results" {
  name = "sqs-results-send"
  role = aws_iam_role.task_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "sqs:SendMessage"
        Resource = var.results_queue_arn
      }
    ]
  })
}

resource "aws_iam_role_policy" "task_cloudwatch" {
  name = "cloudwatch-metrics"
  role = aws_iam_role.task_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "cloudwatch:PutMetricData"
        Resource = "*"
        Condition = {
          StringEquals = {
            "cloudwatch:namespace" = "PMFEngine"
          }
        }
      }
    ]
  })
}

resource "aws_iam_role_policy" "task_logs" {
  name = "cloudwatch-logs"
  role = aws_iam_role.task_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = [
          aws_cloudwatch_log_group.broker.arn,
          "${aws_cloudwatch_log_group.broker.arn}:*"
        ]
      }
    ]
  })
}

# --- Security Group ---

resource "aws_security_group" "broker" {
  name        = "broker-sg-${var.environment}"
  description = "Security group for PMF Broker ECS service"
  vpc_id      = var.vpc_id

  tags = {
    Name        = "PMF Broker"
    Environment = var.environment
  }
}

resource "aws_security_group_rule" "broker_ingress_alb" {
  type                     = "ingress"
  from_port                = 8080
  to_port                  = 8080
  protocol                 = "tcp"
  description              = "Allow broker ALB to reach broker tasks"
  security_group_id        = aws_security_group.broker.id
  source_security_group_id = aws_security_group.broker_alb.id
}

# --- ALB security group ---

resource "aws_security_group" "broker_alb" {
  name        = "broker-alb-sg-${var.environment}"
  description = "Security group for PMF Broker internal ALB"
  vpc_id      = var.vpc_id

  tags = {
    Name        = "PMF Broker ALB"
    Environment = var.environment
  }
}

resource "aws_security_group_rule" "broker_alb_ingress_agent" {
  count                    = var.agent_security_group_id != "" ? 1 : 0
  type                     = "ingress"
  from_port                = 443
  to_port                  = 443
  protocol                 = "tcp"
  description              = "Allow agent tasks to reach broker ALB (HTTPS)"
  security_group_id        = aws_security_group.broker_alb.id
  source_security_group_id = var.agent_security_group_id
}

resource "aws_security_group_rule" "broker_alb_ingress_dispatch" {
  count                    = var.dispatch_lambda_security_group_id != "" ? 1 : 0
  type                     = "ingress"
  from_port                = 443
  to_port                  = 443
  protocol                 = "tcp"
  description              = "Allow dispatch Lambda to reach broker ALB (HTTPS)"
  security_group_id        = aws_security_group.broker_alb.id
  source_security_group_id = var.dispatch_lambda_security_group_id
}

resource "aws_security_group_rule" "broker_alb_egress_to_broker" {
  type                     = "egress"
  from_port                = 8080
  to_port                  = 8080
  protocol                 = "tcp"
  description              = "Allow ALB to forward to broker tasks"
  security_group_id        = aws_security_group.broker_alb.id
  source_security_group_id = aws_security_group.broker.id
}

resource "aws_security_group_rule" "broker_egress_https" {
  type              = "egress"
  from_port         = 443
  to_port           = 443
  protocol          = "tcp"
  description       = "HTTPS for Anthropic, Databricks, Tavily, Gemini APIs via NAT"
  security_group_id = aws_security_group.broker.id
  cidr_blocks       = ["0.0.0.0/0"]
}

resource "aws_security_group_rule" "broker_egress_dns" {
  type              = "egress"
  from_port         = 53
  to_port           = 53
  protocol          = "udp"
  description       = "DNS resolution via VPC DNS"
  security_group_id = aws_security_group.broker.id
  cidr_blocks       = [data.aws_vpc.selected.cidr_block]
}

# --- Internal ALB ---
# The ALB fronts the broker service. Traffic flow:
#   runner/dispatch → ALB (https://broker-<env>.ai.goodparty.org:443) → broker tasks (:8080)
# ALB is internal (unreachable from the public internet) and terminates TLS
# with a public ACM cert; backend traffic ALB → task is plaintext 8080 over
# VPC ENIs, which is the standard AWS pattern.
#
# Public DNS exposes the hostname (via CT logs and the A record) but not the
# service itself — the internal ALB has no public-routable IP. The tradeoff:
# observers can see `broker-<env>.ai.goodparty.org` exists and resolves to a
# 10.x private IP. Mild info disclosure, zero attack surface change.

resource "aws_lb" "broker" {
  name               = "broker-${var.environment}"
  internal           = true
  load_balancer_type = "application"
  security_groups    = [aws_security_group.broker_alb.id]
  subnets            = var.private_subnet_ids

  # Streaming Anthropic responses can be 30-60s; give them room during drain.
  idle_timeout                     = 300
  enable_cross_zone_load_balancing = true

  tags = {
    Environment = var.environment
  }
}

resource "aws_lb_target_group" "broker" {
  name        = "broker-${var.environment}"
  port        = 8080
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = var.vpc_id

  # Generous drain so in-flight streams finish before a scale-in stops a task.
  deregistration_delay = 120

  health_check {
    enabled             = true
    path                = "/health"
    port                = "traffic-port"
    protocol            = "HTTP"
    matcher             = "200"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  tags = {
    Environment = var.environment
  }
}

# --- ACM cert (public, DNS-validated in goodparty.org) ---

resource "aws_acm_certificate" "broker" {
  domain_name       = local.broker_hostname
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }

  tags = {
    Environment = var.environment
  }
}

resource "aws_route53_record" "broker_cert_validation" {
  # Key on domain_name — it's the only field plan-knowable for a new cert.
  # `dvo.resource_record_name` is computed after cert creation, so Terraform
  # can't use it as a for_each key at plan time. Canonical AWS pattern.
  for_each = {
    for dvo in aws_acm_certificate.broker.domain_validation_options :
    dvo.domain_name => {
      name   = dvo.resource_record_name
      record = dvo.resource_record_value
      type   = dvo.resource_record_type
    }
  }

  allow_overwrite = true
  name            = each.value.name
  records         = [each.value.record]
  ttl             = 60
  type            = each.value.type
  zone_id         = var.public_zone_id
}

resource "aws_acm_certificate_validation" "broker" {
  certificate_arn         = aws_acm_certificate.broker.arn
  validation_record_fqdns = [for r in aws_route53_record.broker_cert_validation : r.fqdn]
}

# --- Public DNS ALIAS → internal ALB ---
# Public record pointing at the internal ALB. Resolvable from anywhere but
# only reachable from inside the VPC (ALB has no public IP).

resource "aws_route53_record" "broker" {
  # Publish DNS only after the HTTPS listener is live. Without this, Terraform
  # may create the public ALIAS before the listener exists — clients resolving
  # during rollout hit connection-refused.
  depends_on = [aws_lb_listener.broker]

  zone_id = var.public_zone_id
  name    = local.broker_hostname
  type    = "A"

  alias {
    name                   = aws_lb.broker.dns_name
    zone_id                = aws_lb.broker.zone_id
    evaluate_target_health = true
  }
}

# --- HTTPS listener ---

resource "aws_lb_listener" "broker" {
  load_balancer_arn = aws_lb.broker.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = aws_acm_certificate_validation.broker.certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.broker.arn
  }
}

# --- ECS Cluster ---

resource "aws_ecs_cluster" "broker" {
  name = "broker-${var.environment}"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }

  tags = {
    Environment = var.environment
  }
}

# --- ECS Task Definition ---

resource "aws_ecs_task_definition" "broker" {
  family                   = "broker-${var.environment}"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "4096"
  memory                   = "8192"
  execution_role_arn       = aws_iam_role.task_execution_role.arn
  task_role_arn            = aws_iam_role.task_role.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }

  container_definitions = jsonencode([
    {
      name  = "broker"
      image = "${var.ecr_repository_url}:${var.docker_image_tag}"

      portMappings = [
        {
          containerPort = 8080
          hostPort      = 8080
          protocol      = "tcp"
          name          = "broker"
        }
      ]

      # Allow 2 minutes for graceful shutdown so in-flight streaming Anthropic
      # responses can finish before SIGKILL during scale-in.
      stopTimeout = 120

      healthCheck = {
        command     = ["CMD-SHELL", "curl -f http://localhost:8080/health || exit 1"]
        interval    = 30
        timeout     = 5
        retries     = 3
        startPeriod = 15
      }

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.broker.name
          "awslogs-region"        = data.aws_region.current.name
          "awslogs-stream-prefix" = "ecs"
        }
      }

      environment = [
        {
          name  = "ENVIRONMENT"
          value = var.environment
        },
        {
          name  = "DYNAMO_TABLE_NAME"
          value = aws_dynamodb_table.scope_tickets.name
        },
        {
          name  = "RESULTS_QUEUE_URL"
          value = var.results_queue_url
        },
        {
          name  = "ARTIFACT_BUCKET"
          value = var.artifact_bucket_name
        },
        {
          name  = "EXPERIMENT_METADATA_BUCKET"
          value = var.experiment_metadata_bucket_name
        }
      ]

      secrets = [
        {
          name      = "ANTHROPIC_API_KEY"
          valueFrom = "${aws_secretsmanager_secret.broker.arn}:ANTHROPIC_API_KEY::"
        },
        {
          name      = "GEMINI_API_KEY"
          valueFrom = "${aws_secretsmanager_secret.broker.arn}:GEMINI_API_KEY::"
        },
        {
          name      = "TAVILY_API_KEY"
          valueFrom = "${aws_secretsmanager_secret.broker.arn}:TAVILY_API_KEY::"
        },
        {
          name      = "DATABRICKS_SERVER_HOSTNAME"
          valueFrom = "${aws_secretsmanager_secret.broker.arn}:DATABRICKS_SERVER_HOSTNAME::"
        },
        {
          name      = "DATABRICKS_HTTP_PATH"
          valueFrom = "${aws_secretsmanager_secret.broker.arn}:DATABRICKS_HTTP_PATH::"
        },
        {
          name      = "DATABRICKS_API_KEY"
          valueFrom = "${aws_secretsmanager_secret.broker.arn}:DATABRICKS_API_KEY::"
        },
        {
          name      = "SERVICE_TOKEN_HASH"
          valueFrom = "${aws_secretsmanager_secret.broker.arn}:SERVICE_TOKEN_HASH::"
        },
        {
          name      = "CLERK_SECRET_KEY"
          valueFrom = "${aws_secretsmanager_secret.broker.arn}:CLERK_SECRET_KEY::"
        },
        {
          name      = "CLERK_FRONTEND_API_BASE"
          valueFrom = "${aws_secretsmanager_secret.broker.arn}:CLERK_FRONTEND_API_BASE::"
        },
        {
          name      = "GP_API_BASE_URL"
          valueFrom = "${aws_secretsmanager_secret.broker.arn}:GP_API_BASE_URL::"
        },
        {
          name      = "AGENT_FLEET_CLERK_ID"
          valueFrom = "${aws_secretsmanager_secret.broker.arn}:AGENT_FLEET_CLERK_ID::"
        },
        {
          name      = "AGENT_MCP_TOKEN_SECRET"
          valueFrom = "${aws_secretsmanager_secret.broker.arn}:AGENT_MCP_TOKEN_SECRET::"
        },
        {
          name      = "BRAINTRUST_API_KEY"
          valueFrom = "${aws_secretsmanager_secret.broker.arn}:BRAINTRUST_API_KEY::"
        }
      ]
    }
  ])

  tags = {
    Environment = var.environment
  }
}

# --- ECS Service ---

resource "aws_ecs_service" "broker" {
  name                              = "broker-${var.environment}"
  cluster                           = aws_ecs_cluster.broker.id
  task_definition                   = aws_ecs_task_definition.broker.arn
  desired_count                     = 1
  launch_type                       = "FARGATE"
  enable_execute_command            = true
  health_check_grace_period_seconds = 60

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.broker.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.broker.arn
    container_name   = "broker"
    container_port   = 8080
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  # desired_count is managed by application-autoscaling after initial apply.
  # Without this, terraform plan will always want to reset it to 1.
  lifecycle {
    ignore_changes = [desired_count]
  }

  tags = {
    Environment = var.environment
  }
}

# --- Autoscaling ---

variable "autoscale_min_capacity" {
  description = "Minimum number of broker tasks held by application-autoscaling. Defaults to 1; prod overrides to keep warm capacity."
  type        = number
  default     = 1
}

variable "autoscale_max_capacity" {
  description = "Maximum number of broker tasks application-autoscaling may scale to under load. Defaults to 10; prod overrides for more burst headroom."
  type        = number
  default     = 10
}

resource "aws_appautoscaling_target" "broker" {
  min_capacity       = var.autoscale_min_capacity
  max_capacity       = var.autoscale_max_capacity
  resource_id        = "service/${aws_ecs_cluster.broker.name}/${aws_ecs_service.broker.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}

resource "aws_appautoscaling_policy" "broker_cpu" {
  name               = "broker-${var.environment}-cpu-target"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.broker.resource_id
  scalable_dimension = aws_appautoscaling_target.broker.scalable_dimension
  service_namespace  = aws_appautoscaling_target.broker.service_namespace

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
    target_value       = 55
    scale_out_cooldown = 60
    scale_in_cooldown  = 300
  }
}

resource "aws_appautoscaling_policy" "broker_memory" {
  name               = "broker-${var.environment}-memory-target"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.broker.resource_id
  scalable_dimension = aws_appautoscaling_target.broker.scalable_dimension
  service_namespace  = aws_appautoscaling_target.broker.service_namespace

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageMemoryUtilization"
    }
    target_value       = 70
    scale_out_cooldown = 60
    scale_in_cooldown  = 300
  }
}

# ECS Exec requires the task role to have SSM Messages permissions so the
# SSM agent in the task can open a control channel back to AWS.
resource "aws_iam_role_policy" "task_ecs_exec" {
  name = "ecs-exec-ssm-messages"
  role = aws_iam_role.task_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "ssmmessages:CreateControlChannel",
          "ssmmessages:CreateDataChannel",
          "ssmmessages:OpenControlChannel",
          "ssmmessages:OpenDataChannel",
        ]
        Resource = "*"
      },
    ]
  })
}

# --- DNS Firewall: Agent Rule Group ---
# Restricts agent tasks to resolving only the broker hostname.

resource "aws_route53_resolver_firewall_domain_list" "agent_allow" {
  name    = "broker-agent-allow-${var.environment}"
  domains = ["${local.broker_hostname}."]

  tags = {
    Environment = var.environment
  }
}

resource "aws_route53_resolver_firewall_domain_list" "agent_block_all" {
  name    = "broker-agent-block-all-${var.environment}"
  domains = ["*."]

  tags = {
    Environment = var.environment
  }
}

resource "aws_route53_resolver_firewall_rule_group" "agent" {
  name = "broker-agent-dns-${var.environment}"

  tags = {
    Environment = var.environment
  }
}

resource "aws_route53_resolver_firewall_rule" "agent_allow_broker" {
  name                    = "allow-broker"
  action                  = "ALLOW"
  firewall_domain_list_id = aws_route53_resolver_firewall_domain_list.agent_allow.id
  firewall_rule_group_id  = aws_route53_resolver_firewall_rule_group.agent.id
  priority                = 100
}

resource "aws_route53_resolver_firewall_rule" "agent_block_all" {
  name                    = "block-all"
  action                  = "BLOCK"
  block_response          = "NXDOMAIN"
  firewall_domain_list_id = aws_route53_resolver_firewall_domain_list.agent_block_all.id
  firewall_rule_group_id  = aws_route53_resolver_firewall_rule_group.agent.id
  priority                = 200
}

# Agent DNS firewall rule group is defined but NOT associated with the shared
# VPC. Its BLOCK-all default rule would break DNS resolution for every other
# service in the VPC. Associate it only when agent tasks run in a dedicated VPC.

# --- DNS Firewall: Broker Rule Group ---
# Allows the broker to resolve only the external APIs and AWS services it needs.

resource "aws_route53_resolver_firewall_domain_list" "broker_allow" {
  name = "broker-upstream-allow-${var.environment}"
  domains = [
    "api.anthropic.com.",
    "*.databricks.com.",
    "api.tavily.com.",
    "generativelanguage.googleapis.com.",
    "s3.us-west-2.amazonaws.com.",
    "sqs.us-west-2.amazonaws.com.",
    "dynamodb.us-west-2.amazonaws.com.",
    "secretsmanager.us-west-2.amazonaws.com.",
  ]

  tags = {
    Environment = var.environment
  }
}

resource "aws_route53_resolver_firewall_rule_group" "broker" {
  name = "broker-dns-${var.environment}"

  tags = {
    Environment = var.environment
  }
}

resource "aws_route53_resolver_firewall_rule" "broker_allow_apis" {
  name                    = "allow-apis"
  action                  = "ALLOW"
  firewall_domain_list_id = aws_route53_resolver_firewall_domain_list.broker_allow.id
  firewall_rule_group_id  = aws_route53_resolver_firewall_rule_group.broker.id
  priority                = 100
}

resource "aws_route53_resolver_firewall_rule" "broker_block_all" {
  name                    = "block-all"
  action                  = "BLOCK"
  block_response          = "NXDOMAIN"
  firewall_domain_list_id = aws_route53_resolver_firewall_domain_list.agent_block_all.id
  firewall_rule_group_id  = aws_route53_resolver_firewall_rule_group.broker.id
  priority                = 200
}

# Broker DNS firewall rule group is defined but NOT associated with the shared
# VPC. Like the agent rule group, its BLOCK-all default (priority 200) would
# break DNS resolution for every other service in the VPC. Associate it only
# when the broker runs in a dedicated VPC.

# --- CloudWatch Alarms ---

resource "aws_cloudwatch_metric_alarm" "broker_5xx" {
  count               = var.sns_topic_arn != "" ? 1 : 0
  alarm_name          = "broker-5xx-${var.environment}"
  alarm_description   = "PMF Broker returning 5xx errors"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "Broker5xxCount"
  namespace           = "PMFEngine"
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  alarm_actions       = [var.sns_topic_arn]

  dimensions = {
    Environment = var.environment
  }

  tags = {
    Environment = var.environment
  }
}

resource "aws_cloudwatch_metric_alarm" "service_token_auth_failure" {
  count               = var.sns_topic_arn != "" ? 1 : 0
  alarm_name          = "broker-service-token-auth-failure-${var.environment}"
  alarm_description   = "PMF Broker service token authentication failure"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  metric_name         = "BrokerServiceTokenAuthFailure"
  namespace           = "PMFEngine"
  period              = 300
  statistic           = "Sum"
  threshold           = 1
  alarm_actions       = [var.sns_topic_arn]

  dimensions = {
    Environment = var.environment
  }

  tags = {
    Environment = var.environment
  }
}

resource "aws_cloudwatch_metric_alarm" "run_token_auth_failure" {
  count               = var.sns_topic_arn != "" ? 1 : 0
  alarm_name          = "broker-run-token-auth-failure-${var.environment}"
  alarm_description   = "PMF Broker run token authentication failures exceeding threshold"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  metric_name         = "BrokerRunTokenAuthFailure"
  namespace           = "PMFEngine"
  period              = 300
  statistic           = "Sum"
  threshold           = 3
  alarm_actions       = [var.sns_topic_arn]

  dimensions = {
    Environment = var.environment
  }

  tags = {
    Environment = var.environment
  }
}

resource "aws_cloudwatch_metric_alarm" "classifier_exception" {
  count               = var.sns_topic_arn != "" ? 1 : 0
  alarm_name          = "broker-classifier-exception-${var.environment}"
  alarm_description   = "PMF Broker classifier threw an exception"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "BrokerClassifierException"
  namespace           = "PMFEngine"
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  alarm_actions       = [var.sns_topic_arn]

  dimensions = {
    Environment = var.environment
  }

  tags = {
    Environment = var.environment
  }
}

resource "aws_cloudwatch_metric_alarm" "experiment_terminal_failure" {
  count               = var.sns_topic_arn != "" ? 1 : 0
  alarm_name          = "broker-experiment-terminal-failure-${var.environment}"
  alarm_description   = "PMF experiment terminated with failed/contract_violation/timeout. Emitted by broker /internal/run-status — alarms on every occurrence so ops sees user-impacting failures in Slack within minutes (otherwise the only signal is a row in experiment_run.status=FAILED that nobody queries proactively)."
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  metric_name         = "ExperimentTerminalFailure"
  namespace           = "PMFEngine"
  period              = 300
  statistic           = "Sum"
  threshold           = 1
  treat_missing_data  = "notBreaching"
  alarm_actions       = [var.sns_topic_arn]
  ok_actions          = [var.sns_topic_arn]

  dimensions = {
    Environment = var.environment
  }

  tags = {
    Environment = var.environment
  }
}

resource "aws_cloudwatch_metric_alarm" "broker_task_count" {
  count               = var.sns_topic_arn != "" ? 1 : 0
  alarm_name          = "broker-task-count-${var.environment}"
  alarm_description   = "PMF Broker Fargate running task count dropped below 1"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 2
  metric_name         = "RunningTaskCount"
  namespace           = "ECS/ContainerInsights"
  period              = 60
  statistic           = "Average"
  threshold           = 1
  alarm_actions       = [var.sns_topic_arn]

  dimensions = {
    ClusterName = aws_ecs_cluster.broker.name
    ServiceName = aws_ecs_service.broker.name
  }

  tags = {
    Environment = var.environment
  }
}

resource "aws_cloudwatch_metric_alarm" "dynamodb_errors" {
  count               = var.sns_topic_arn != "" ? 1 : 0
  alarm_name          = "broker-dynamodb-errors-${var.environment}"
  alarm_description   = "DynamoDB errors on PMF scope-tickets table"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "SystemErrors"
  namespace           = "AWS/DynamoDB"
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  alarm_actions       = [var.sns_topic_arn]

  dimensions = {
    TableName = aws_dynamodb_table.scope_tickets.name
  }

  tags = {
    Environment = var.environment
  }
}

# Note: results DLQ depth alarm is in the control-plane module (same queue family)

# --- Outputs ---

output "cluster_arn" {
  value       = aws_ecs_cluster.broker.arn
  description = "Broker ECS cluster ARN"
}

output "service_name" {
  value       = aws_ecs_service.broker.name
  description = "Broker ECS service name"
}

output "security_group_id" {
  # Consumers (dispatch Lambda, runner) must now open egress to the ALB SG,
  # not the task SG, because the ALB sits in between. Swapped here so the
  # downstream remote_state lookups pick up the right target automatically.
  value       = aws_security_group.broker_alb.id
  description = "SG that clients (dispatch Lambda, runner) need egress permission to — this is the broker ALB SG"
}

output "broker_tasks_security_group_id" {
  value       = aws_security_group.broker.id
  description = "SG of the broker ECS tasks themselves (for debugging / direct task access)"
}

output "dynamodb_table_name" {
  value       = aws_dynamodb_table.scope_tickets.name
  description = "DynamoDB scope tickets table name"
}

output "broker_url" {
  value       = local.broker_url
  description = "HTTPS URL for the broker (public DNS, internal ALB)"
}

output "broker_hostname" {
  value       = local.broker_hostname
  description = "Fully-qualified broker hostname (used in DNS firewall allow-list and SG debugging)"
}

output "agent_dns_firewall_rule_group_id" {
  value       = aws_route53_resolver_firewall_rule_group.agent.id
  description = "Route53 DNS Firewall rule group ID for agent tasks"
}

output "broker_dns_firewall_rule_group_id" {
  value       = aws_route53_resolver_firewall_rule_group.broker.id
  description = "Route53 DNS Firewall rule group ID for broker"
}

output "secrets_arn" {
  value       = aws_secretsmanager_secret.broker.arn
  description = "ARN of the broker secrets in Secrets Manager"
}

output "broker_alb_dns_name" {
  value       = aws_lb.broker.dns_name
  description = "Internal ALB DNS name (clients should use the https hostname via broker_url, not this directly)"
}

output "broker_alb_security_group_id" {
  value       = aws_security_group.broker_alb.id
  description = "Security group ID of the broker ALB"
}
