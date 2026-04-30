# Plan: Move QA Module into gp-ai-projects

## Context

The `meeting_briefings_qa` repo is a standalone QA auditor that validates AI-generated meeting briefings against source data. It reads briefings from S3, runs deterministic structural checks + LLM-based claim adjudication, and outputs Block/OK routing decisions.

Currently it lives in a separate repo and is run manually. The goal is to move it into `gp-ai-projects` as a workspace member and wire it up as an automated Lambda triggered after briefing generation.

## Current State

- **Source:** `meeting_briefings_qa/qa/` (~2,500 lines)
- **Trigger point:** `meeting_pipeline/lambda_handlers/process.py` already has `QA_QUEUE_URL = os.environ.get("QA_QUEUE_URL", "")` but never sends to it
- **Dependencies:** anthropic, google-genai, openai, pymupdf, boto3, openpyxl
- **LLM judges:** Claude Sonnet 4.6 (triage) + Gemini 2.5 Flash (escalation)
- **Inputs:** briefing.json, normalized.json, optional haystaq.json, optional PDF bytes (all from S3)
- **Outputs:** qa_summary.md, review_log.xlsx, trace.json (written to S3)

## Target Architecture

```
gp-ai-projects/
├── meeting_pipeline/          # existing — discover, scan, collect, extract, briefing
├── meeting_qa/                # NEW — QA auditor
│   ├── pyproject.toml         # workspace member, depends on gp-shared
│   ├── qa/
│   │   ├── engine/            # runner, decision, config, models
│   │   ├── inputs/            # project specs (meeting briefing adapter)
│   │   ├── extraction/        # claim extraction + type taxonomy
│   │   ├── evidence/          # PDF grounding, citation matching
│   │   ├── adjudication/      # phase 1 triage + phase 2 escalation
│   │   ├── checks/            # deterministic validation
│   │   └── reporting/         # summary, review log, trace
│   ├── lambda_handler.py      # SQS-triggered Lambda entry point
│   ├── scripts/
│   │   └── run_qa.py          # CLI for manual runs
│   └── tests/
├── shared/                    # existing — llm_gemini, etc.
└── pyproject.toml             # workspace root — add meeting_qa as member
```

## Implementation Steps

### Step 1: Move code
- Copy `meeting_briefings_qa/qa/` → `gp-ai-projects/meeting_qa/qa/`
- Copy `meeting_briefings_qa/scripts/run_qa.py` → `meeting_qa/scripts/run_qa.py`
- Copy `meeting_briefings_qa/tests/` → `meeting_qa/tests/`
- Create `meeting_qa/pyproject.toml` with dependencies
- Add `"meeting_qa"` to workspace members in root `pyproject.toml`

### Step 2: Create Lambda handler
Create `meeting_qa/lambda_handler.py`:
- Polls SQS queue (same pattern as `meeting_pipeline/lambda_handlers/scan.py`)
- Message format: `{"briefing_key": "meeting_pipeline/output/briefings/chapel-hill-NC_2026-04-29_briefing.json"}`
- Loads briefing + normalized + PDF from S3
- Runs QA engine
- Writes results to S3 at `meeting_pipeline/output/qa/{slug}_{date}_trace.json`
- Updates briefing metadata with QA status (Block/OK)

### Step 3: Wire up the trigger
In `meeting_pipeline/lambda_handlers/process.py`:
- After briefing generation succeeds, send message to `QA_QUEUE_URL` with the briefing S3 key
- Only send if `QA_QUEUE_URL` is configured (graceful no-op if not set)

### Step 4: Infrastructure
- Create SQS queue: `meeting-qa-queue`
- Create Lambda function: `meeting-qa-process`
- Wire: process Lambda → SQS → QA Lambda
- IAM: QA Lambda needs S3 read/write + Secrets Manager read
- Secrets: add `ANTHROPIC_API_KEY` to existing AI_SECRETS (for Claude judge)

### Step 5: Dockerfile
- Create `meeting_qa/Dockerfile` (standard Python Lambda, no Playwright/Chromium needed)
- Much simpler than the discover Dockerfile

## What NOT to change
- Don't merge QA logic into the briefing generation stage — keep them independent
- Don't modify the QA engine's internal architecture — it's well-structured
- Don't remove `check_provenance()` / `check_fiscal_amounts()` from generate.py — they're fast pre-delivery sanity checks, QA is the full audit

## Decision Points
- **Block behavior:** When QA returns Block, should the briefing be deleted from S3, flagged, or moved to a quarantine prefix?
- **Retry policy:** Should blocked briefings be automatically regenerated, or flagged for human review?
- **Cost:** Each QA run uses 2 LLM calls (triage + escalation). At 200 cities/week, that's ~400 LLM calls/week for QA alone. Acceptable?
- **Latency:** QA adds ~30-60s per briefing. Run async (SQS) or inline before delivery?

## Branch Strategy
- Branch: `feature/qa-integration`
- Base: `develop`
- PR scope: move code + Lambda handler + SQS trigger wiring
- Infrastructure (SQS queue, Lambda, IAM) tracked separately or in Terraform
