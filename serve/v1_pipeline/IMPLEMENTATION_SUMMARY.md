# Poll Analysis Pipeline - Implementation Summary

## Overview

The poll analysis pipeline supports two modes for analyzing poll responses:

| Mode | Use Case | Processing | Example |
|------|----------|------------|---------|
| **cluster** | Open-ended questions | Hierarchical clustering discovers themes | "What issues matter?" |
| **classify** | Structured questions | LLM classifies into predefined options | "Do you support the park? Yes/No" |

## Backwards Compatibility

Both modes are **fully backwards compatible**:

- Same CSV input format (phone_number, message_text, sent_at)
- Same output JSON structure (`pollAnalysisComplete` event)
- Same output: S3 (`output/events/`) + SQS (`pollAnalysisComplete`)
- `cluster` mode works unchanged (default for CLI and S3 trigger)

Trigger mechanisms:
- **S3 upload**: Triggers `cluster` mode (legacy, for open-ended questions)
- **SQS message**: Triggers either mode based on `mode` field (recommended)

## Architecture

```
S3 TRIGGER (cluster mode - legacy):
┌──────────┐    ┌────────────┐    ┌─────────────────┐    ┌──────────────┐    ┌──────────────┐
│ CSV      │───▶│ Lambda     │───▶│ ECS Task        │───▶│ S3 output/   │───▶│ SQS Output   │
│ S3       │    │ (S3 event) │    │ (clustering)    │    │ events/*.json│    │ Queue        │
└──────────┘    └────────────┘    └─────────────────┘    └──────────────┘    └──────────────┘

SQS TRIGGER (cluster or classify mode - recommended):
┌──────────┐    ┌────────────┐    ┌─────────────────┐    ┌──────────────┐    ┌──────────────┐
│ SQS      │───▶│ Lambda     │───▶│ ECS Task        │───▶│ S3 output/   │───▶│ SQS Output   │
│ Trigger  │    │ (SQS event)│    │ (cluster/classify)│  │ events/*.json│    │ Queue        │
└──────────┘    └────────────┘    └─────────────────┘    └──────────────┘    └──────────────┘
     │
     └── Contains: mode, poll_id, s3_key, question_text, options[]
```

## Code Structure

```
serve/v1_pipeline/
├── pipeline/
│   ├── orchestrator.py         # cluster mode orchestrator
│   ├── classifier.py           # classify mode pipeline
│   ├── event_saver.py          # Shared output saving
│   └── sqs_publisher.py        # Event publishing (uses event_saver)
├── scripts/
│   └── run_pipeline.py         # CLI entry point (supports both modes)
├── input/                      # Input CSVs
└── output/
    └── events/                 # Output JSON (same for both modes)
```

## Classify Mode Flow

```
1. LOAD
   ├── Read CSV (same format as cluster mode)
   └── Get options from CLI args (or SQS message in production)

2. CLASSIFY (ResponseClassifier)
   │
   │  For EACH message, parallel LLM call:
   │  ┌────────────────────────────────────────────────────┐
   │  │ Question: "Do you support the park?"               │
   │  │ Options: ["Yes", "No"]                             │
   │  │ Response: "yes we need more green space"           │
   │  │                                                    │
   │  │ → {"option": "Yes", "confidence": 0.95}            │
   │  └────────────────────────────────────────────────────┘
   │
   │  Classification rules:
   │  ├── Match predefined option → "Yes", "No", etc.
   │  ├── Unclear/off-topic → "Other"
   │  └── Opt-out keywords (stop, unsubscribe) → filtered out
   │
   │  100 concurrent LLM calls via ThreadPoolExecutor
   │
   └── Output: option + confidence per message

3. BUCKET
   │  Group by option, track quotes:
   │  ├── Yes:   {count: 88, quotes: [...]}
   │  ├── No:    {count: 95, quotes: [...]}
   │  └── Other: {count: 37, quotes: [...]}
   │
   └── Sort by count (descending) for ranking

4. SUMMARIZE (BucketSummarizer)
   │
   │  For EACH bucket, LLM generates:
   │  ├── summary: 1-2 sentence overview
   │  └── analysis: 2-3 paragraph detailed analysis
   │
   └── Parallel summarization across all buckets

5. OUTPUT
   │
   ├── Save to S3: output/events/events_{timestamp}.json
   └── Publish to SQS: pollAnalysisComplete event
```

## Output Format

Both modes produce the same output structure:

```json
[
  {
    "type": "pollAnalysisComplete",
    "data": {
      "pollId": "park-vote-test",
      "totalResponses": 220,
      "issues": [
        {
          "pollId": "park-vote-test",
          "rank": 1,
          "theme": "Yes",
          "summary": "Supporters cite need for green space and family recreation.",
          "analysis": "Detailed 2-3 paragraph analysis of Yes responses...",
          "quotes": [
            {"quote": "yes we need more parks", "phone_number": "5551000001"},
            {"quote": "absolutely, great for families", "phone_number": "5551000002"}
          ],
          "responseCount": 88
        },
        {
          "rank": 2,
          "theme": "No",
          "summary": "Opposition focused on traffic, parking, and tax concerns.",
          "analysis": "Detailed analysis of No responses...",
          "quotes": [...],
          "responseCount": 95
        },
        {
          "rank": 3,
          "theme": "Other",
          "summary": "Unclear or off-topic responses.",
          "analysis": "...",
          "quotes": [...],
          "responseCount": 37
        }
      ]
    }
  }
]
```

**Key mapping:**
- `cluster` mode `theme` = discovered cluster name ("Roads and Infrastructure")
- `classify` mode `theme` = predefined option name ("Yes", "No", "Roads", etc.)

## Technology Stack

| Component | Technology | Notes |
|-----------|------------|-------|
| LLM | Gemini Flash | `thinking_budget=0` for cost efficiency |
| Parallelism | `ThreadPoolExecutor` + `asyncio` | 100+ concurrent LLM calls |
| Output | JSON | `output/events/events_{timestamp}.json` |
| Infrastructure | AWS SQS + Lambda + Step Functions + ECS | SQS trigger for both modes |

## Question Types Supported

### 1. Yes/No Questions
```bash
--question-text "Do you support the park?"
--options-json '["Yes", "No"]'
```
Responses like "yes", "yep", "absolutely", "no way" → classified into Yes/No/Other

### 2. Multiple Choice
```bash
--question-text "What is your top priority?"
--options-json '["Roads", "Schools", "Safety", "Taxes", "Housing"]'
```
Responses classified into predefined options + Other bucket

### 3. Open-Ended (cluster mode)
```bash
--mode cluster
```
No predefined options → discovers themes via hierarchical clustering

## CLI Commands

### Cluster Mode: Open-Ended Clustering (Default)

```bash
# Standard run
DISABLE_SQS_PUBLISH=true uv run serve/v1_pipeline/scripts/run_pipeline.py \
  --campaign berkley_consolidated

# With debug logging
DISABLE_SQS_PUBLISH=true uv run serve/v1_pipeline/scripts/run_pipeline.py \
  --campaign berkley_consolidated \
  --debug
```

### Classify Mode: Yes/No Classification

```bash
  uv run serve/v1_pipeline/scripts/run_pipeline.py \
    --campaign park-vote-test \
    --mode classify \
    --poll-id park-vote-test \
    --question-text "Do you support the new park project?" \
    --options-json '["Yes", "No"]' \
  --debug
```

### Classify Mode: Multiple Choice Classification

```bash
uv run serve/v1_pipeline/scripts/run_pipeline.py \
  --campaign berkley-issues-test \
  --mode classify \
  --poll-id berkley-issues-test \
  --question-text "What is your top priority for the city?" \
  --options-json '["Roads", "Taxes", "Schools", "Safety", "Traffic"]' \
  --debug
```

### Output Location

Both modes:
1. Save to S3: `output/events/events_{timestamp}.json`
2. Publish to SQS: `pollAnalysisComplete` event to output queue

## Test Data

| File | Messages | Type |
|------|----------|------|
| `input/park-vote-test.csv` | 230 | Yes/No with reasons |
| `input/berkley-issues-test.csv` | 100 | Multi-category issues |
| `input/berkley/*.csv` | ~300 | Open-ended (cluster) |

## Infrastructure Changes (Terraform)

SQS trigger adds:
- SQS queue: `serve-analyze-trigger-{env}` (FIFO)
- Dead letter queue: `serve-analyze-trigger-dlq-{env}`
- Lambda event source mapping for SQS
- IAM policies for SQS access

Lambda handler routes by event type:
- S3 event → `cluster` mode (legacy)
- SQS event → `cluster` or `classify` mode based on `mode` field

## Key Files Changed

| File | Changes |
|------|---------|
| `pipeline/classifier.py` | New file - classify mode logic |
| `pipeline/event_saver.py` | New file - shared output saving |
| `pipeline/sqs_publisher.py` | Uses event_saver for output |
| `scripts/run_pipeline.py` | Added `--mode classify` support |
| `entrypoint.sh` | Routes to cluster or classify based on `PIPELINE_MODE` |
| `lambda-trigger/index.ts` | Handles both S3 and SQS events |
| `main.tf` | SQS queue and Lambda trigger |
| `step-function-definition.json` | DLQ support for classify failures |

## Error Handling

- **Classification failures**: Individual messages that fail classification go to "Other" bucket
- **Pipeline failures**: Message goes to DLQ for retry, SNS notification sent
- **DLQ**: Failed SQS messages retained 14 days for debugging/reprocessing

## Cost Considerations

- Gemini Flash with `thinking_budget=0`: ~$0.075/1M tokens
- 100 messages × ~50 tokens/classification ≈ 5K tokens ≈ $0.0004
- Summarization adds ~1K tokens per bucket
- Total cost per poll: typically < $0.01

## Future Enhancements

1. **Demographics cross-tabs**: Break down results by age, location, etc.
2. **Rating scales**: Support 1-5 or 1-10 numeric ratings
3. **Mixed questions**: "Do you support? Why or why not?"
4. **Auto-detection**: Infer question type from response patterns
