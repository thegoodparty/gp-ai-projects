# Meeting Pipeline Infrastructure Plan

## Overview

Event-driven pipeline on AWS. Two Lambdas (scan + process), one SQS queue, and Fargate for discovery. Scan runs daily; everything else is triggered by events.

---

## Architecture

```
                          ┌─────────────┐
                          │  Onboard    │  (manual / API call)
                          │  new city   │
                          └──────┬──────┘
                                 │ SQS: discover-queue
                                 ▼
                          ┌─────────────┐
                          │  DISCOVER   │  Fargate (Playwright + Firecrawl)
                          │  per city   │  → writes source.json + verifies
                          └─────────────┘

       ┌──────────────────────────────────────────────┐
       │  EventBridge (daily cron, 6 AM UTC)          │
       │  → Step Function (scan-fanout)               │
       │    → lists verified cities                   │
       │    → invokes Scan Lambda ×N (max 10 concurrent) │
       └──────────────────┬───────────────────────────┘
                          │
                          ▼
                   ┌─────────────┐
                   │    SCAN     │  Lambda (httpx API calls)
                   │  per city   │  → writes upcoming_meetings.json
                   └──────┬──────┘
                          │ For each newly posted future agenda
                          │ SQS: process-queue (one msg per meeting)
                          ▼
                   ┌─────────────┐
                   │   PROCESS   │  Lambda (15 min timeout)
                   │ per meeting │  → collect PDF
                   │             │  → extract (Gemini)
                   │             │  → generate briefing (Gemini)
                   │             │  → basic inline checks
                   │             │  → writes to briefings/
                   └──────┬──────┘
                          │ SQS: qa-queue
                          ▼
                   ┌─────────────┐
                   │     QA      │  Lambda (separate image)
                   │ per briefing│  meeting_briefings_qa repo
                   │             │  → full claim verification
                   └──────┬──────┘
                          │
                   ┌──────┴──────┐
                   │             │
                   ▼             ▼
              🟢 OK          🔴 Block
        (briefings_approved/)  (SNS alert)
```

---

## Components

| Component | Type | Purpose |
|-----------|------|---------|
| **Scan Lambda** | Lambda | Scan one city for meetings, send posted to process queue |
| **Process Lambda** | Lambda | Collect + extract + brief for one meeting, send to QA queue |
| **QA Lambda** | Lambda (separate image) | Full QA from meeting_briefings_qa repo, approve or block |
| **Process Queue** | SQS + DLQ | Buffer between scan and process (one msg per meeting) |
| **QA Queue** | SQS + DLQ | Buffer between process and QA (one msg per briefing) |
| **Discover Task** | Fargate | Source discovery with Playwright (on-demand via SQS) |
| **Discover Queue** | SQS + DLQ | Trigger discover for new/rediscovery cities |
| **Step Function** | Step Functions | Daily scan fan-out (list cities → Map → Scan Lambda) |
| **EventBridge** | EventBridge | Daily cron at 6 AM UTC |
| **SNS Topic** | SNS | QA block alerts + DLQ failure alerts |

---

## Flow

1. **Daily 6 AM UTC**: EventBridge triggers Step Function
2. **Step Function**: calls Scan Lambda with `action: "list_cities"` → gets verified city slugs → Map state invokes Scan Lambda per city (max 10 concurrent)
3. **Scan Lambda**: scans one city, compares with previous scan, sends newly posted future meetings to `process-queue`
4. **Process Lambda** (SQS triggered): for one meeting:
   - Downloads the agenda PDF
   - Extracts text (PyMuPDF, Firecrawl OCR fallback)
   - Gemini structured extraction → normalized JSON
   - 3-pass Gemini briefing generation
   - Basic inline checks (priority count, headlines)
   - Writes briefing to `briefings/`
   - Sends briefing key to `qa-queue`
5. **QA Lambda** (SQS triggered, separate Docker image from meeting_briefings_qa repo):
   - Full claim verification against source material
   - If OK → copy to `briefings_approved/`
   - If blocked → SNS alert

---

## Verification Gate

Only verified cities flow through the pipeline. Verification status is stored in `source.json`:

| Status | Meaning | Pipeline |
|--------|---------|----------|
| `verified` | Downloaded real agenda PDF with text | Scan + Process |
| `verified_ocr_needed` | Scanned PDF (needs OCR) | Scan + Process |
| `verified_non_pdf` | Non-PDF document format | Scan + Process |
| `unverified` / not set | Not proven to work | Skipped |

---

## S3 Layout

```
sources/{slug}/manifest.json              ← One-time (onboarding)
sources/{slug}/source.json                ← Discovery result + verification
sources/{slug}/upcoming_meetings.json     ← Scan result
sources/{slug}/data/{platform}/*.pdf      ← Collected PDFs
output/normalized/{slug}_{date}.json      ← Extracted agenda items
output/briefings/{slug}_{date}_briefing.json       ← Raw briefing
output/briefings_approved/{slug}_{date}_briefing.json  ← QA-approved
output/qa/{slug}_{date}_qa_summary.json   ← QA result
```

---

## Cost Estimate (daily, ~108 verified cities)

| Component | Daily Cost |
|-----------|-----------|
| Scan Lambda (~108 invocations) | ~$0.03 |
| Process Lambda (~20-30 meetings with new agendas) | ~$0.50 + $2 Gemini |
| SQS | ~$0.01 |
| Step Function | ~$0.01 |
| S3 storage | ~$0.50/month |
| **Total daily** | **~$3** |
| **Total monthly** | **~$90** |

Discovery (on-demand): ~$5-10 per batch.

---

## Prerequisites

1. Add API keys to `AI_SECRETS_DEV` in Secrets Manager
2. VPC/subnet IDs for Fargate (same as serve-analyze)
3. ECR repository for Docker images
4. `terraform apply` to create resources
5. Push Docker image via GitHub Actions
