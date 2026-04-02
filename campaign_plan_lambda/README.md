# Campaign Plan Lambda

AWS Lambda that finds community events for political candidates using Gemini 3 with Google Search grounding. Triggered by SQS, writes results to S3, and notifies gp-api via SQS.

## What it does

1. Receives campaign ID, election date, city, and state from gp-api via SQS
2. Uses Gemini 3 with Google Search grounding to find real community events
3. Filters and structures the best 5-8 events as tasks
4. Writes the result JSON to S3
5. Sends a completion message to gp-api's SQS queue

## Local testing

### Prerequisites

- Python 3.12+
- Project venv activated: `source .venv/bin/activate` (from the `gp-ai-projects` root)
- `GEMINI_API_KEY` set in `.env` at the project root

### Run the SQS harness

The harness simulates the full Lambda flow locally — real Gemini API calls, mocked AWS services (S3, SQS).

```bash
cd /gp-ai-projects
source .venv/bin/activate
python campaign_plan_lambda/test_sqs_harness.py
```

This takes ~30-40 seconds and outputs:
- `campaign_plan_lambda/test_sqs_output.json` — the full result JSON (gitignored)
- Console output showing the SQS completion message and task list

### Run unit tests

```bash
source .venv/bin/activate
python -m pytest campaign_plan_lambda/tests/ -v
```

## Deploying to Lambda

### Build the zip

```bash
cd /gp-ai-projects
bash campaign_plan_lambda/build.sh
```

This creates `campaign_plan_lambda/lambda.zip` (~9MB zipped, ~28MB unzipped).

### Upload to Lambda (dev)

```bash
aws lambda update-function-code \
  --function-name campaign-plan-dev \
  --zip-file fileb://campaign_plan_lambda/lambda.zip \
  --region us-west-2 \
  --profile gp-engineer
```

Code deploys also happen automatically via GitHub Actions when changes are pushed to `develop`, `qa`, or `prod` branches.

### Infrastructure changes

Infrastructure is managed by Terraform in `infrastructure/modules/campaign-plan-lambda/` and `infrastructure/environments/dev/campaign-plan-lambda/`.

```bash
cd infrastructure/environments/dev/campaign-plan-lambda
terraform plan -out=tfplan
terraform apply tfplan
```

## Testing in AWS (dev)

### Send a test message

```bash
aws sqs send-message \
  --queue-url https://sqs.us-west-2.amazonaws.com/333022194791/campaign-plan-input-dev.fifo \
  --message-body '{"campaignId":99999,"election_date":"2026-11-04","city":"Boston","state":"MA"}' \
  --message-group-id "test-$(uuidgen)" \
  --message-deduplication-id "$(uuidgen)" \
  --region us-west-2 \
  --profile gp-engineer
```

Use a unique `message-group-id` per campaign to allow parallel processing.

### Watch logs

```bash
aws logs tail /aws/lambda/campaign-plan-dev --follow --region us-west-2 --profile gp-readonly
```

### Check S3 output

```bash
aws s3 ls s3://campaign-plan-results-dev/results/99999/ --region us-west-2 --profile gp-readonly
```

## SQS message format

### Input (from gp-api)

```json
{
  "campaignId": 12345,
  "election_date": "2026-11-04",
  "city": "Boston",
  "state": "MA"
}
```

### Output (to gp-api — success)

```json
{
  "type": "campaignPlanComplete",
  "data": {
    "campaignId": 12345,
    "status": "completed",
    "s3Key": "results/12345/2026-04-02T15:30:00-abc12345.json",
    "taskCount": 7,
    "generationTimestamp": "2026-04-02T15:30:00.000000+00:00"
  }
}
```

### Output (to gp-api — error, only on final retry)

```json
{
  "type": "campaignPlanComplete",
  "data": {
    "campaignId": 12345,
    "status": "error",
    "error": "Campaign plan generation failed"
  }
}
```

### S3 result JSON

```json
{
  "campaignId": 12345,
  "tasks": [
    {
      "title": "Boston Pride Festival and Parade",
      "description": "Large celebration offering engagement with diverse voters.",
      "cta": "Attend event",
      "flowType": "events",
      "week": 22,
      "date": "2026-06-06"
    }
  ],
  "taskCount": 7,
  "generationTimestamp": "2026-04-02T15:30:00.000000+00:00"
}
```

## Architecture

- **Input queue:** `campaign-plan-input-{env}.fifo` (FIFO, batch size 1)
- **Dead letter queue:** `campaign-plan-dlq-{env}.fifo` (after 3 failed attempts)
- **S3 bucket:** `campaign-plan-results-{env}` (12-month retention)
- **Output queue:** gp-api's existing `{env}-Queue.fifo` (shared with other services)
- **Alerts:** DLQ alarm → SNS → shared Slack notifier
- **Runtime:** Python 3.12, 1GB memory, 15-minute timeout
- **AI:** Gemini 3 Flash with Google Search grounding (2 API calls per invocation)
