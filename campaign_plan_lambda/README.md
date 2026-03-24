# Campaign Plan Lambda

AWS Lambda service that generates campaign plans and extracts community event tasks for political candidates. Triggered by SQS, writes results to S3, and notifies gp-api via SQS.

## What it does

1. Receives campaign info from gp-api via SQS FIFO queue
2. Uses Gemini with Google Search grounding to find real community events
3. Generates a campaign timeline and voter contact plan
4. Extracts event tasks from the timeline
5. Writes the full result JSON to S3
6. Sends a completion message to gp-api's SQS queue

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

This takes ~60-90 seconds and outputs:
- `campaign_plan_lambda/test_sqs_output.json` — the full result JSON (gitignored)
- Console output showing the SQS completion message and task counts

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

This creates `campaign_plan_lambda/lambda.zip` (~30MB zipped, ~94MB unzipped).

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
  --message-body '{"campaignId":99999,"campaignInfo":{"candidate_name":"Test McTestface","election_date":"2026-11-04","office_and_jurisdiction":"City Council District 3 Boston MA","race_type":"Nonpartisan","incumbent_status":"N/A","seats_available":1,"number_of_opponents":3,"win_number":10000,"total_likely_voters":40000,"available_cell_phones":10000,"available_landlines":10000}}' \
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

### Check queue status

```bash
aws sqs get-queue-attributes \
  --queue-url https://sqs.us-west-2.amazonaws.com/333022194791/campaign-plan-input-dev.fifo \
  --attribute-names All \
  --region us-west-2 \
  --profile gp-readonly
```

## SQS message format

### Input (from gp-api)

```json
{
  "campaignId": 12345,
  "campaignInfo": {
    "candidate_name": "Jane Rivera",
    "election_date": "2026-11-04",
    "office_and_jurisdiction": "City Council District 3 Boston MA",
    "race_type": "Nonpartisan",
    "incumbent_status": "N/A",
    "seats_available": 1,
    "number_of_opponents": 2,
    "win_number": 8000,
    "total_likely_voters": 30000,
    "available_cell_phones": 9000,
    "available_landlines": 5000,
    "primary_date": null,
    "additional_race_context": null
  }
}
```

### Output (to gp-api — success)

```json
{
  "type": "campaignPlanComplete",
  "data": {
    "campaignId": 12345,
    "status": "completed",
    "s3Key": "results/12345/2026-03-31T02:05:04-abc12345.json",
    "taskCount": 9,
    "generationTimestamp": "2026-03-31T02:05:04.529431+00:00"
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

## Architecture

- **Input queue:** `campaign-plan-input-dev.fifo` (FIFO, batch size 1)
- **Dead letter queue:** `campaign-plan-dlq-dev.fifo` (after 3 failed attempts)
- **S3 bucket:** `campaign-plan-results-dev` (12-month retention)
- **Output queue:** gp-api's `develop-Queue.fifo` (shared with other services)
- **Alerts:** DLQ alarm → SNS → shared Slack notifier
- **Runtime:** Python 3.12, 1GB memory, 15-minute timeout
