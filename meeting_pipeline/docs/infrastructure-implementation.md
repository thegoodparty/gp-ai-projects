# Meeting Pipeline Infrastructure — Technical Implementation Guide

## Overview

This document describes exactly how to deploy the meeting pipeline to AWS, following the existing patterns in `infrastructure/modules/`. It covers Terraform modules, Lambda packaging, Fargate tasks, SQS queues, and CI/CD.

---

## Architecture

```
EventBridge (daily 6 AM UTC)
  → Step Function (scan-fan-out)
    → Scan Lambda ×201 (concurrency=10)
      → SQS: collect-queue (cities with posted agendas)
        → Collect Lambda (concurrency=5)
          → SQS: extract-queue (per meeting with PDF)
            → Extract Lambda (concurrency=5)
              → SQS: briefing-queue (per normalized meeting)
                → Briefing Lambda (concurrency=3)
                  → SQS: qa-queue (per briefing)
                    → QA Lambda (concurrency=3)

SQS: discover-queue (on-demand: onboard, rediscovery)
  → Discover Fargate task (concurrency=3)
```

---

## 1. Terraform Module Structure

Create `infrastructure/modules/meeting-pipeline/` following the campaign-plan-lambda pattern:

```
infrastructure/modules/meeting-pipeline/
├── main.tf              # All resources
├── variables.tf         # Input variables
├── outputs.tf           # Exported values
├── lambdas.tf           # Lambda function definitions (split from main for readability)
├── queues.tf            # SQS queues + DLQs
├── fargate.tf           # ECS cluster + task definition for Discover
└── step-function.json   # ASL definition for scan fan-out
```

### variables.tf

```hcl
variable "environment" {
  type        = string
  description = "Deployment environment (dev, qa, prod)"
}

variable "vpc_id" {
  type        = string
  description = "VPC ID for Fargate tasks"
}

variable "private_subnet_ids" {
  type        = list(string)
  description = "Private subnet IDs for Fargate tasks"
}

variable "ecr_repository_url" {
  type        = string
  description = "ECR repository URL for Discover Fargate image"
}

variable "docker_image_tag" {
  type        = string
  default     = "meeting-pipeline-dev"
}

variable "s3_bucket_name" {
  type        = string
  description = "S3 bucket for pipeline data (e.g. meeting-pipeline-dev)"
}

variable "failure_notification_email" {
  type        = string
  default     = ""
  description = "Email for DLQ failure alerts"
}

variable "shared_slack_notifier_lambda_arn" {
  type        = string
  default     = ""
  description = "ARN of shared Slack notifier Lambda"
}
```

### Resource Naming Convention

Follow existing pattern: `meeting-pipeline-{resource}-{environment}`

```
Lambda:     meeting-pipeline-scan-dev
            meeting-pipeline-collect-dev
            meeting-pipeline-extract-dev
            meeting-pipeline-briefing-dev
            meeting-pipeline-qa-dev
SQS:        meeting-pipeline-collect-dev          (standard, not FIFO)
            meeting-pipeline-collect-dlq-dev
            meeting-pipeline-extract-dev
            meeting-pipeline-extract-dlq-dev
            meeting-pipeline-briefing-dev
            meeting-pipeline-briefing-dlq-dev
            meeting-pipeline-qa-dev
            meeting-pipeline-qa-dlq-dev
            meeting-pipeline-discover-dev
            meeting-pipeline-discover-dlq-dev
ECS:        meeting-pipeline-discover-dev
Step Fn:    meeting-pipeline-scan-fanout-dev
EventBridge: meeting-pipeline-daily-scan-dev
SNS:        meeting-pipeline-failures-dev
CloudWatch: /aws/lambda/meeting-pipeline-scan-dev
            /ecs/meeting-pipeline-discover-dev
```

---

## 2. SQS Queues

5 standard queues + 5 DLQs. Standard (not FIFO) because order doesn't matter and we want max throughput.

```hcl
# queues.tf — one queue per downstream stage

locals {
  queue_configs = {
    collect = {
      visibility_timeout = 900   # 15 min (Lambda timeout)
      max_receive_count  = 3
    }
    extract = {
      visibility_timeout = 300   # 5 min
      max_receive_count  = 3
    }
    briefing = {
      visibility_timeout = 300   # 5 min
      max_receive_count  = 3
    }
    qa = {
      visibility_timeout = 300   # 5 min
      max_receive_count  = 3
    }
    discover = {
      visibility_timeout = 1800  # 30 min (Fargate timeout)
      max_receive_count  = 3
    }
  }
}

resource "aws_sqs_queue" "dlq" {
  for_each = local.queue_configs

  name                      = "meeting-pipeline-${each.key}-dlq-${var.environment}"
  message_retention_seconds = 1209600  # 14 days

  tags = {
    Environment = var.environment
    Project     = "meeting-pipeline"
  }
}

resource "aws_sqs_queue" "queue" {
  for_each = local.queue_configs

  name                       = "meeting-pipeline-${each.key}-${var.environment}"
  visibility_timeout_seconds = each.value.visibility_timeout
  message_retention_seconds  = 1209600  # 14 days
  receive_wait_time_seconds  = 20       # Long polling

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq[each.key].arn
    maxReceiveCount     = each.value.max_receive_count
  })

  tags = {
    Environment = var.environment
    Project     = "meeting-pipeline"
  }
}
```

### Message Formats

```json
// collect-queue (sent by Scan Lambda)
{"slug": "chapel-hill-NC"}

// extract-queue (sent by Collect Lambda)
{"slug": "chapel-hill-NC", "date": "2026-04-29", "platform": "legistar"}

// briefing-queue (sent by Extract Lambda)
{"normalized_key": "meeting_pipeline/output/normalized/chapel-hill-NC_2026-04-29.json"}

// qa-queue (sent by Briefing Lambda)
{"briefing_key": "meeting_pipeline/output/briefings/chapel-hill-NC_2026-04-29_briefing.json"}

// discover-queue (sent by onboard script or collection failure)
{"slug": "chapel-hill-NC", "city": "Chapel Hill", "state": "NC", "reason": "onboard"}
```

---

## 3. Lambda Functions

### Packaging: Container Image (recommended)

The meeting pipeline has heavy dependencies (PyMuPDF, google-genai, httpx, beautifulsoup4, pydantic, firecrawl-py) that exceed the 250MB zip limit when combined. Use Lambda container images (10GB limit).

**One shared image for all 5 Lambdas** — different handler entry points.

#### Dockerfile

Create `meeting_pipeline/Dockerfile.lambda`:

```dockerfile
# Build stage
FROM python:3.12-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY meeting_pipeline/ meeting_pipeline/
COPY shared/ shared/

# Install only meeting-pipeline deps (no playwright/browser-use for Lambda)
RUN uv sync --frozen --no-dev

# Runtime stage
FROM python:3.12-slim

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/meeting_pipeline /app/meeting_pipeline
COPY --from=builder /app/shared /app/shared

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH="/app"

# Lambda runtime interface client
RUN pip install awslambdaric

# Default handler — overridden per function in Terraform
ENTRYPOINT ["python", "-m", "awslambdaric"]
CMD ["meeting_pipeline.lambda_handlers.scan.handler"]
```

#### Lambda Handlers

Create `meeting_pipeline/lambda_handlers/`:

```python
# meeting_pipeline/lambda_handlers/__init__.py
"""Lambda handlers for each pipeline stage."""

# meeting_pipeline/lambda_handlers/_secrets.py
"""Shared secrets loading — follows campaign-plan-lambda pattern."""
import json
import os
import boto3

_cache = None

def inject_secrets():
    """Load API keys from Secrets Manager into env vars."""
    global _cache
    if _cache:
        return
    
    environment = os.environ.get("ENVIRONMENT", "dev").upper()
    client = boto3.client("secretsmanager")
    response = client.get_secret_value(SecretId=f"AI_SECRETS_{environment}")
    secrets = json.loads(response["SecretString"])
    
    for key in ["GEMINI_API_KEY", "SERPER_API_KEY", "FIRECRAWL_API_KEY", "TAVILY_API_KEY"]:
        if key in secrets:
            os.environ[key] = secrets[key]
    
    _cache = True
```

```python
# meeting_pipeline/lambda_handlers/scan.py
import asyncio
import json
import os
import boto3
from meeting_pipeline.lambda_handlers._secrets import inject_secrets
from meeting_pipeline.shared.config import AgentConfig, get_storage
from meeting_pipeline.stages.scan.process import process_one_city

sqs = boto3.client("sqs")

def handler(event, context):
    inject_secrets()
    cfg = AgentConfig.from_env()
    storage = get_storage(cfg)
    
    slug = event["slug"]
    source_key = f"{cfg.sources_prefix}/{slug}/source.json"
    source = storage.read_json(source_key)
    
    result = asyncio.run(
        process_one_city(slug, source, source_key, storage=storage)
    )
    
    um_key = f"{cfg.sources_prefix}/{slug}/upcoming_meetings.json"
    
    # Compare with previous scan to detect new agendas
    prev_posted = set()
    if storage.exists(um_key):
        prev = storage.read_json(um_key)
        prev_posted = {
            m["date"] for m in prev.get("upcoming", [])
            if m.get("agenda_posted")
        }
    
    storage.write_json(um_key, result)
    
    # Send newly posted meetings to collect queue
    collect_queue_url = os.environ.get("COLLECT_QUEUE_URL")
    if collect_queue_url:
        new_posted = [
            m for m in result.get("upcoming", [])
            if m.get("agenda_posted") and m.get("date") not in prev_posted
        ]
        if new_posted:
            sqs.send_message(
                QueueUrl=collect_queue_url,
                MessageBody=json.dumps({"slug": slug}),
            )
    
    return {
        "slug": slug,
        "meetings": len(result.get("upcoming", [])),
        "posted": sum(1 for m in result.get("upcoming", []) if m.get("agenda_posted")),
    }
```

```python
# meeting_pipeline/lambda_handlers/collect.py
import asyncio
import json
import os
import boto3
from meeting_pipeline.lambda_handlers._secrets import inject_secrets
from meeting_pipeline.shared.config import AgentConfig, get_storage
from meeting_pipeline.stages.collect.process import process_one_city

sqs = boto3.client("sqs")

def handler(event, context):
    inject_secrets()
    cfg = AgentConfig.from_env()
    storage = get_storage(cfg)
    
    for record in event.get("Records", [event]):
        body = json.loads(record["body"]) if "body" in record else record
        slug = body["slug"]
        
        source = storage.read_json(f"{cfg.sources_prefix}/{slug}/source.json")
        city = source.get("city", slug)
        state = source.get("state", "")
        
        result = asyncio.run(process_one_city(city, state, cfg=cfg, storage=storage))
        
        # Send each posted meeting to extract queue
        extract_queue_url = os.environ.get("EXTRACT_QUEUE_URL")
        if extract_queue_url:
            um = storage.read_json(f"{cfg.sources_prefix}/{slug}/upcoming_meetings.json")
            for m in um.get("upcoming", []):
                if m.get("agenda_posted"):
                    sqs.send_message(
                        QueueUrl=extract_queue_url,
                        MessageBody=json.dumps({
                            "slug": slug,
                            "date": m["date"],
                            "platform": um.get("platform", ""),
                        }),
                    )
```

```python
# meeting_pipeline/lambda_handlers/extract.py
import asyncio
import json
import os
import boto3
from meeting_pipeline.lambda_handlers._secrets import inject_secrets
from meeting_pipeline.shared.config import AgentConfig, get_storage
from meeting_pipeline.stages.extract.normalize import (
    extract_pdf_text, find_best_pdf, extract_with_gemini, normalize_meeting,
)

sqs = boto3.client("sqs")

def handler(event, context):
    inject_secrets()
    cfg = AgentConfig.from_env()
    storage = get_storage(cfg)
    
    # Ensure project root on path for shared.llm_gemini
    import sys
    from pathlib import Path
    _root = str(Path(__file__).resolve().parent.parent.parent)
    if _root not in sys.path:
        sys.path.insert(0, _root)
    
    from shared.llm_gemini import GeminiClient, GeminiModelType
    gemini = GeminiClient(default_model=GeminiModelType.FLASH_LITE)
    
    for record in event.get("Records", [event]):
        body = json.loads(record["body"]) if "body" in record else record
        slug = body["slug"]
        meeting_date = body["date"]
        platform = body.get("platform", "")
        
        # Read city info from upcoming_meetings
        um = storage.read_json(f"{cfg.sources_prefix}/{slug}/upcoming_meetings.json")
        city = um.get("city", slug)
        state = um.get("state", "")
        body_name = um.get("body", "")
        
        # Find the meeting
        meeting = next(
            (m for m in um.get("upcoming", []) if m.get("date") == meeting_date),
            None,
        )
        if not meeting:
            continue
        
        # Find PDF
        pdf_key, pdf_label = find_best_pdf(
            slug, meeting_date, platform, storage, cfg.sources_prefix
        )
        if not pdf_key:
            continue
        
        # Extract
        pdf_bytes = storage.read_bytes(pdf_key)
        text = extract_pdf_text(pdf_bytes)
        extraction = extract_with_gemini(text, city, state, meeting_date, gemini)
        
        # Normalize
        official = {"name": "", "city": city, "state": state, "role": body_name or "City Council"}
        meeting_for_norm = {
            "date": meeting_date,
            "title": meeting.get("title", ""),
            "body": body_name,
            "source_url": meeting.get("agenda_url", ""),
            "agenda_files": [{"name": "Agenda", "type": "Agenda", "url": meeting.get("agenda_url", "")}] if meeting.get("agenda_url") else [],
        }
        normalized = normalize_meeting(
            official=official, meeting=meeting_for_norm, extraction=extraction,
            pdf_key=pdf_key, pdf_label=pdf_label, city_slug=slug, platform=platform,
        )
        
        out_key = f"{cfg.output_prefix}/normalized/{slug}_{meeting_date}.json"
        storage.write_json(out_key, normalized)
        
        # Send to briefing queue
        briefing_queue_url = os.environ.get("BRIEFING_QUEUE_URL")
        if briefing_queue_url:
            sqs.send_message(
                QueueUrl=briefing_queue_url,
                MessageBody=json.dumps({"normalized_key": out_key}),
            )
```

```python
# meeting_pipeline/lambda_handlers/briefing.py
import json
import os
import boto3
from meeting_pipeline.lambda_handlers._secrets import inject_secrets
from meeting_pipeline.shared.config import AgentConfig, get_storage
from meeting_pipeline.stages.briefing.generate import generate_briefing_for_meeting

sqs = boto3.client("sqs")

def handler(event, context):
    inject_secrets()
    cfg = AgentConfig.from_env()
    storage = get_storage(cfg)
    
    for record in event.get("Records", [event]):
        body = json.loads(record["body"]) if "body" in record else record
        normalized_key = body["normalized_key"]
        
        result = generate_briefing_for_meeting(normalized_key, storage, cfg)
        
        # Send to QA queue
        qa_queue_url = os.environ.get("QA_QUEUE_URL")
        if qa_queue_url and result.get("status") == "ok":
            briefing_key = result.get("briefing_key", "")
            if briefing_key:
                sqs.send_message(
                    QueueUrl=qa_queue_url,
                    MessageBody=json.dumps({"briefing_key": briefing_key}),
                )
```

```python
# meeting_pipeline/lambda_handlers/qa.py
import json
import os
import boto3
from meeting_pipeline.lambda_handlers._secrets import inject_secrets
from meeting_pipeline.shared.config import AgentConfig, get_storage

sqs = boto3.client("sqs")

def handler(event, context):
    inject_secrets()
    cfg = AgentConfig.from_env()
    storage = get_storage(cfg)
    
    for record in event.get("Records", [event]):
        body = json.loads(record["body"]) if "body" in record else record
        briefing_key = body["briefing_key"]
        
        # Run QA checks (import from meeting_briefings_qa)
        from qa.engine.decision import run_qa_checks
        
        briefing = storage.read_json(briefing_key)
        qa_result = run_qa_checks(briefing, storage, cfg)
        
        # Write QA result
        qa_key = briefing_key.replace("briefings/", "qa/").replace("_briefing.json", "_qa_summary.json")
        storage.write_json(qa_key, qa_result)
        
        # If OK, copy to approved folder
        if qa_result.get("status") == "ok":
            approved_key = briefing_key.replace("briefings/", "briefings_approved/")
            storage.write_json(approved_key, briefing)
        else:
            # Alert on block
            sns_arn = os.environ.get("FAILURE_SNS_TOPIC_ARN")
            if sns_arn:
                sns = boto3.client("sns")
                slug = briefing_key.split("/")[-1].split("_")[0]
                sns.publish(
                    TopicArn=sns_arn,
                    Subject=f"Meeting briefing blocked: {slug}",
                    Message=json.dumps(qa_result, indent=2),
                )
```

---

## 4. Lambda Terraform Resources

```hcl
# lambdas.tf

locals {
  lambda_functions = {
    scan = {
      handler = "meeting_pipeline.lambda_handlers.scan.handler"
      timeout = 300   # 5 min
      memory  = 512
      environment = {
        COLLECT_QUEUE_URL = aws_sqs_queue.queue["collect"].url
      }
    }
    collect = {
      handler = "meeting_pipeline.lambda_handlers.collect.handler"
      timeout = 900   # 15 min
      memory  = 1024
      environment = {
        EXTRACT_QUEUE_URL = aws_sqs_queue.queue["extract"].url
      }
      sqs_trigger = "collect"
    }
    extract = {
      handler = "meeting_pipeline.lambda_handlers.extract.handler"
      timeout = 300   # 5 min
      memory  = 1024
      environment = {
        BRIEFING_QUEUE_URL = aws_sqs_queue.queue["briefing"].url
      }
      sqs_trigger = "extract"
    }
    briefing = {
      handler = "meeting_pipeline.lambda_handlers.briefing.handler"
      timeout = 300   # 5 min
      memory  = 1024
      environment = {
        QA_QUEUE_URL = aws_sqs_queue.queue["qa"].url
      }
      sqs_trigger = "briefing"
    }
    qa = {
      handler = "meeting_pipeline.lambda_handlers.qa.handler"
      timeout = 300   # 5 min
      memory  = 1024
      environment = {
        FAILURE_SNS_TOPIC_ARN = aws_sns_topic.failures.arn
      }
      sqs_trigger = "qa"
    }
  }
}

resource "aws_lambda_function" "stage" {
  for_each = local.lambda_functions

  function_name = "meeting-pipeline-${each.key}-${var.environment}"
  role          = aws_iam_role.lambda.arn
  package_type  = "Image"
  image_uri     = "${var.ecr_repository_url}:${var.docker_image_tag}"
  timeout       = each.value.timeout
  memory_size   = each.value.memory

  image_config {
    command = [each.value.handler]
  }

  environment {
    variables = merge(
      {
        ENVIRONMENT      = var.environment
        S3_BUCKET        = var.s3_bucket_name
        STORAGE_BACKEND  = "s3"
        SOURCES_PREFIX   = "meeting_pipeline/sources"
        OUTPUT_PREFIX    = "meeting_pipeline/output"
      },
      each.value.environment,
    )
  }

  tags = {
    Environment = var.environment
    Project     = "meeting-pipeline"
  }
}

# SQS triggers for collect, extract, briefing, qa
resource "aws_lambda_event_source_mapping" "sqs_trigger" {
  for_each = {
    for k, v in local.lambda_functions : k => v if lookup(v, "sqs_trigger", "") != ""
  }

  event_source_arn = aws_sqs_queue.queue[each.value.sqs_trigger].arn
  function_name    = aws_lambda_function.stage[each.key].arn
  batch_size       = 1
}
```

---

## 5. Step Function for Scan Fan-Out

The daily scan needs to invoke the Scan Lambda for each city. Use a Step Function with a Map state.

### step-function.json

```json
{
  "Comment": "Meeting pipeline daily scan fan-out",
  "StartAt": "ListCities",
  "States": {
    "ListCities": {
      "Type": "Task",
      "Resource": "arn:aws:states:::lambda:invoke",
      "Parameters": {
        "FunctionName": "${scan_lambda_arn}",
        "Payload": {
          "action": "list_cities"
        }
      },
      "ResultSelector": {
        "cities.$": "$.Payload.cities"
      },
      "Next": "ScanAllCities"
    },
    "ScanAllCities": {
      "Type": "Map",
      "ItemsPath": "$.cities",
      "MaxConcurrency": 10,
      "Iterator": {
        "StartAt": "ScanOneCity",
        "States": {
          "ScanOneCity": {
            "Type": "Task",
            "Resource": "arn:aws:states:::lambda:invoke",
            "Parameters": {
              "FunctionName": "${scan_lambda_arn}",
              "Payload.$": "$"
            },
            "Retry": [
              {
                "ErrorEquals": ["States.TaskFailed"],
                "IntervalSeconds": 60,
                "MaxAttempts": 2,
                "BackoffRate": 2.0
              }
            ],
            "Catch": [
              {
                "ErrorEquals": ["States.ALL"],
                "Next": "ScanFailed"
              }
            ],
            "End": true
          },
          "ScanFailed": {
            "Type": "Pass",
            "Result": {"status": "failed"},
            "End": true
          }
        }
      },
      "Next": "Done"
    },
    "Done": {
      "Type": "Succeed"
    }
  }
}
```

Note: The Scan Lambda handler needs a `list_cities` mode that returns all city slugs from S3. When `event.action == "list_cities"`, it lists `source.json` files and returns `{"cities": [{"slug": "..."}, ...]}`.

### EventBridge Rule

```hcl
resource "aws_cloudwatch_event_rule" "daily_scan" {
  name                = "meeting-pipeline-daily-scan-${var.environment}"
  schedule_expression = "cron(0 6 * * ? *)"  # 6 AM UTC daily

  tags = {
    Environment = var.environment
    Project     = "meeting-pipeline"
  }
}

resource "aws_cloudwatch_event_target" "step_function" {
  rule      = aws_cloudwatch_event_rule.daily_scan.name
  target_id = "scan-fanout"
  arn       = aws_sfn_state_machine.scan_fanout.arn
  role_arn  = aws_iam_role.eventbridge.arn
}
```

---

## 6. Discover Fargate Task

Follow the serve-analyze-fargate pattern. Discover needs Playwright + Chromium.

### Dockerfile.discover

```dockerfile
FROM python:3.12-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY meeting_pipeline/ meeting_pipeline/
COPY shared/ shared/

RUN uv sync --frozen --no-dev

# Runtime stage with Playwright
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    wget gnupg libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxrandr2 \
    libgbm1 libpango-1.0-0 libasound2 libxshmfence1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/meeting_pipeline /app/meeting_pipeline
COPY --from=builder /app/shared /app/shared

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH="/app"

# Install Playwright Chromium
RUN playwright install chromium

ENTRYPOINT ["python", "-m", "meeting_pipeline.lambda_handlers.discover"]
```

The Discover handler reads from the `discover-queue` SQS queue and processes one city at a time.

### Fargate Terraform

```hcl
# fargate.tf — Discover stage

resource "aws_ecs_cluster" "discover" {
  name = "meeting-pipeline-discover-${var.environment}"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }

  tags = {
    Environment = var.environment
    Project     = "meeting-pipeline"
  }
}

resource "aws_ecs_task_definition" "discover" {
  family                   = "meeting-pipeline-discover-${var.environment}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "4096"
  memory                   = "16384"
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }

  container_definitions = jsonencode([{
    name  = "discover"
    image = "${var.ecr_repository_url}:meeting-pipeline-discover-${var.environment}"
    
    environment = [
      { name = "ENVIRONMENT", value = var.environment },
      { name = "S3_BUCKET", value = var.s3_bucket_name },
      { name = "STORAGE_BACKEND", value = "s3" },
      { name = "SOURCES_PREFIX", value = "meeting_pipeline/sources" },
      { name = "DISCOVER_QUEUE_URL", value = aws_sqs_queue.queue["discover"].url },
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
        "awslogs-group"         = aws_cloudwatch_log_group.discover.name
        "awslogs-region"        = "us-west-2"
        "awslogs-stream-prefix" = "discover"
      }
    }
  }])
}
```

---

## 7. IAM Roles

```hcl
# Lambda execution role (shared by all 5 Lambdas)
resource "aws_iam_role" "lambda" {
  name = "meeting-pipeline-lambda-${var.environment}"

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
    Project     = "meeting-pipeline"
  }
}

# Permissions: CloudWatch, S3, SQS, Secrets Manager
resource "aws_iam_role_policy" "lambda" {
  name = "meeting-pipeline-lambda-${var.environment}"
  role = aws_iam_role.lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = "arn:aws:logs:*:*:*"
      },
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:ListBucket",
          "s3:HeadObject",
          "s3:DeleteObject",
        ]
        Resource = [
          "arn:aws:s3:::${var.s3_bucket_name}",
          "arn:aws:s3:::${var.s3_bucket_name}/meeting_pipeline/*",
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "sqs:SendMessage",
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes",
        ]
        Resource = [for q in aws_sqs_queue.queue : q.arn]
      },
      {
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = "arn:aws:secretsmanager:us-west-2:*:secret:AI_SECRETS_${upper(var.environment)}-*"
      },
    ]
  })
}
```

---

## 8. Monitoring

```hcl
# SNS topic for failures
resource "aws_sns_topic" "failures" {
  name = "meeting-pipeline-failures-${var.environment}"

  tags = {
    Environment = var.environment
    Project     = "meeting-pipeline"
  }
}

# DLQ alarms — one per queue
resource "aws_cloudwatch_metric_alarm" "dlq_alarm" {
  for_each = aws_sqs_queue.dlq

  alarm_name          = "meeting-pipeline-${each.key}-${var.environment}"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ApproximateNumberOfMessagesVisible"
  namespace           = "AWS/SQS"
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  alarm_description   = "Messages in ${each.key} dead letter queue"
  alarm_actions       = [aws_sns_topic.failures.arn]

  dimensions = {
    QueueName = each.value.name
  }
}
```

---

## 9. GitHub Actions Workflow

Create `.github/workflows/build-meeting-pipeline.yml`:

```yaml
name: Build Meeting Pipeline

on:
  push:
    branches: [develop, qa, prod]
    paths:
      - 'meeting_pipeline/**'
      - 'shared/**'
      - 'pyproject.toml'
      - 'uv.lock'
  workflow_dispatch:
    inputs:
      environment:
        description: 'Target environment'
        required: true
        type: choice
        options: [dev, qa, prod]

permissions:
  id-token: write
  contents: read

jobs:
  build-and-push:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      
      - name: Determine environment
        id: env
        run: |
          if [ "${{ github.event_name }}" = "workflow_dispatch" ]; then
            echo "env=${{ inputs.environment }}" >> $GITHUB_OUTPUT
          elif [ "${{ github.ref_name }}" = "prod" ]; then
            echo "env=prod" >> $GITHUB_OUTPUT
          elif [ "${{ github.ref_name }}" = "qa" ]; then
            echo "env=qa" >> $GITHUB_OUTPUT
          else
            echo "env=dev" >> $GITHUB_OUTPUT
          fi

      - name: Configure AWS credentials
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: arn:aws:iam::333022194791:role/github-actions-ecr-push
          aws-region: us-west-2

      - name: Login to ECR
        uses: aws-actions/amazon-ecr-login@v2

      - name: Build and push Lambda image
        uses: docker/build-push-action@v5
        with:
          context: .
          file: meeting_pipeline/Dockerfile.lambda
          push: true
          tags: |
            333022194791.dkr.ecr.us-west-2.amazonaws.com/gp-ai-projects:meeting-pipeline-${{ steps.env.outputs.env }}
          platforms: linux/arm64
          cache-from: type=gha
          cache-to: type=gha,mode=max

      - name: Update Lambda functions
        run: |
          ENV=${{ steps.env.outputs.env }}
          IMAGE="333022194791.dkr.ecr.us-west-2.amazonaws.com/gp-ai-projects:meeting-pipeline-${ENV}"
          for STAGE in scan collect extract briefing qa; do
            aws lambda update-function-code \
              --function-name "meeting-pipeline-${STAGE}-${ENV}" \
              --image-uri "${IMAGE}" \
              --region us-west-2
          done
```

---

## 10. Environment Configuration

Create `infrastructure/environments/dev/meeting-pipeline/main.tf`:

```hcl
terraform {
  backend "s3" {
    bucket       = "goodparty-terraform-state-us-west-2"
    key          = "meeting-pipeline/dev/terraform.tfstate"
    region       = "us-west-2"
    use_lockfile = true
    encrypt      = true
  }
}

provider "aws" {
  region = "us-west-2"
}

data "terraform_remote_state" "shared" {
  backend = "s3"
  config = {
    bucket = "goodparty-terraform-state-us-west-2"
    key    = "shared-infra/dev/terraform.tfstate"
    region = "us-west-2"
  }
}

module "meeting_pipeline" {
  source = "../../../modules/meeting-pipeline"

  environment       = "dev"
  s3_bucket_name    = "meeting-pipeline-dev"
  ecr_repository_url = data.terraform_remote_state.shared.outputs.ecr_repository_url
  vpc_id            = "vpc-XXXXXXXXX"
  private_subnet_ids = ["subnet-XXXXXXXXX", "subnet-XXXXXXXXX"]

  failure_notification_email      = "engineering@goodparty.org"
  shared_slack_notifier_lambda_arn = data.terraform_remote_state.shared.outputs.slack_notifier_lambda_arn
}
```

---

## 11. Implementation Order

### Phase 1: Lambda + SQS (1-2 days)

1. Create `meeting_pipeline/lambda_handlers/` with all 5 handlers
2. Create `meeting_pipeline/Dockerfile.lambda`
3. Create Terraform module: `infrastructure/modules/meeting-pipeline/`
   - Start with just `main.tf`, `variables.tf`, `outputs.tf`
   - 5 Lambda functions + 4 SQS queues (skip discover for now)
4. Create environment config: `infrastructure/environments/dev/meeting-pipeline/main.tf`
5. Create GitHub Actions workflow
6. Deploy to dev, test with 10 cities

### Phase 2: Step Functions + EventBridge (1 day)

1. Add Step Function for scan fan-out
2. Add EventBridge daily cron rule
3. Add `list_cities` mode to scan handler
4. Test end-to-end daily trigger

### Phase 3: Discover Fargate (1-2 days)

1. Create `meeting_pipeline/Dockerfile.discover` with Playwright
2. Add Fargate resources to Terraform module
3. Add discover SQS queue
4. Wire up collection failure → discover queue
5. Test with new city onboard flow

### Phase 4: QA + Monitoring (1 day)

1. Package `meeting_briefings_qa` into the Lambda image
2. Add QA Lambda + qa queue
3. Add DLQ alarms + SNS notifications
4. Add `briefings_approved/` copy logic
5. Test block/approve flow

---

## 12. Pre-Implementation Checklist

- [ ] Full 201-city pipeline test passes locally
- [ ] Confirm `meeting-pipeline-dev` S3 bucket exists and is accessible
- [ ] Add meeting pipeline API keys to `AI_SECRETS_DEV` in Secrets Manager
- [ ] Confirm ECR repository exists for meeting-pipeline images
- [ ] Decide on VPC/subnet IDs for Fargate (reuse serve-analyze networking)
- [ ] Create `meeting-pipeline-lambda` dependency group in pyproject.toml (exclude playwright, browser-use)
- [ ] Test Docker build locally before pushing to ECR
