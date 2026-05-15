# Campaign Plan Lambda

AWS Lambda that finds community events for political candidates using Gemini 3 with Google Search grounding. Triggered by SQS, writes results to S3, and notifies gp-api via SQS. Prompts are managed in Braintrust (project: "campaign-plan") with hardcoded fallbacks.

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
- `GEMINI_API_KEY` and `BRAINTRUST_API_KEY` set in `.env` at the project root

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

### Run with gp-api locally

The local server lets gp-api trigger event generation on your machine instead of going through SQS + Lambda. Real Gemini API calls are made, results are written to dev S3, and completion messages are sent to your personal SQS queue.

**1. Set up `.env` in gp-ai-projects root:**

```
GEMINI_API_KEY=...
AWS_PROFILE=your-admin-profile
AWS_DEFAULT_REGION=us-west-2
S3_RESULTS_BUCKET=campaign-plan-results-dev
OUTPUT_SQS_QUEUE_URL=https://sqs.us-west-2.amazonaws.com/333022194791/YourName_Queue.fifo
```

Each engineer should have their own dedicated SQS queue (e.g. `YourName_Queue.fifo`). This is the same queue your local gp-api reads from (`SQS_QUEUE` in gp-api's `.env`).

**2. Start the local server:**

```bash
cd /gp-ai-projects
source .venv/bin/activate
python campaign_plan_lambda/local_server.py
```

**3. Set up gp-api `.env`:**

```
CAMPAIGN_PLAN_LOCAL_URL=http://localhost:8089/generate
CAMPAIGN_PLAN_RESULTS_BUCKET=campaign-plan-results-dev
```

**4. Start gp-api:** `npm run start:dev`

When a user with no tasks opens the dashboard, gp-api POSTs to `localhost:8089`, the local server runs Gemini, writes to S3, and sends a completion message to your queue. gp-api picks it up and saves the event tasks.

## Deploying to Lambda

### Build the zip

```bash
cd /gp-ai-projects
bash campaign_plan_lambda/build.sh
```

This creates `campaign_plan_lambda/lambda.zip` (~11MB zipped, ~33MB unzipped).

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

## Iterating on prompts via Braintrust evals

The pipeline ships with a Braintrust sandbox eval (`evals.py`) so PMs can iterate on prompts in the Braintrust playground without engineer involvement. Each eval run executes the same `search → filter → cleanup` flow the Lambda runs in prod, against a curated dataset, with the PM-edited prompts substituted in via the playground form.

### One-time setup (per Braintrust project)

In Braintrust UI → `campaign-plan` project → Settings → Environment Variables:

- `GEMINI_API_KEY` — for the sandbox to call Gemini.
- `ENVIRONMENT=eval` — stamps `metadata.environment="eval"` on every span emitted from sandbox runs so they're filterable in Logs separately from prod runs (`DEV` / `QA` / `PROD` come from the Lambda's AWS env config).

You also need a dataset under the same project with rows shaped like `{electionDate, state, city, officeName, officeLevel, primaryElectionDate}`. Drag-to-dataset from the parent `generate_event_tasks` span in any prod trace produces correctly-shaped rows.

### Engineer workflow — push eval changes

After modifying `evals.py`, `event_generator.py`, or anything in `shared/braintrust.py`:

```bash
./campaign_plan_lambda/push_eval_to_braintrust.sh
```

This bundles the eval and uploads it to Braintrust's hosted Lambda sandbox. The script is a Python wrapper around `braintrust push` that works around two SDK quirks (sys.path pollution from uv, and the lazy-load short-circuit when scanning local imports). See the comments in the script for details.

`evals.requirements.txt` is the pip-style manifest the sandbox installs into its venv. Keep it in sync with `pyproject.toml`'s `campaign-plan-lambda` group when bumping versions.

### PM workflow — run the eval

In Braintrust UI:

1. Navigate to `campaign-plan` project → Playground.
2. **+ Task** → **Remote eval** submenu → pick the registered eval.
3. Edit `search_prompt` and/or `filter_prompt` in the form (mustache `{{var}}` placeholders).
4. Pick a dataset, hit **Run**.

Results land as a Braintrust experiment with per-row scores (`count_in_range`, `dates_in_range`, `urls_valid`, `title_overlap`). Compare experiments side-by-side in the UI to evaluate prompt changes.

### Promotion to prod

Once a prompt looks good via eval, save the content into the Braintrust prompt registry under slugs `search-community-events` and `filter-and-structure-events`. The Lambda picks up the latest version on the next invocation via `load_prompt_from_braintrust`.

⚠️ **Prompt updates take effect immediately** on the next Lambda call — there's no environment-pinning yet. Treat the prompt registry as a production-affecting surface and coordinate prompt changes with deploys. Future ticket: wire up Braintrust's deploy-to-environment feature for prompt-version pinning.

### Reading traces

Every Lambda invocation produces a parent `generate_event_tasks` span (type `task`) with two child LLM spans (type `llm`) nested under it:

```
generate_event_tasks                  input = {electionDate, city, state, ...}
                                      output = {tasks: [...]}
                                      metadata.environment = DEV/QA/PROD/eval
                                      metadata.model = gemini-3-flash-preview
├── generate_with_search              input = {prompt: "..."}, output = {text, search_queries, sources}
└── generate_structured_content       input = {prompt: "..."}, output = {events: [...]}
```

Filter the Logs view by `metadata.environment` to isolate prod vs eval runs, or by `metadata.model` to compare model performance. Click into any parent span to drill into the two LLM children for prompt-and-response detail.

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
      "date": "2026-06-06",
      "url": "https://www.bostonprideforthepeople.org"
    }
  ],
  "taskCount": 7,
  "generationTimestamp": "2026-04-02T15:30:00.000000+00:00"
}
```

The `url` field is optional — it is omitted when no direct event link is available.

## Error handling

gp-api should expect three possible outcomes after sending a message to the input queue:

**Success:** gp-api receives a `campaignPlanComplete` message with `status: "completed"`, an S3 key pointing to the result JSON, and a task count. Typical time: 30-40 seconds.

**Generation failure:** If the Gemini API is down or returns an error, the Lambda retries up to 3 times (with ~16 minute gaps between retries). Only on the final failed attempt does gp-api receive a `campaignPlanComplete` message with `status: "error"` and `error: "Campaign plan generation failed"`. If all retries fail, the message also goes to the dead letter queue and a Slack alert fires.

**Invalid message:** If the input message is malformed (bad JSON, missing fields, invalid date format), the Lambda does not retry — the message is consumed immediately. If a `campaignId` can be extracted from the malformed message, gp-api receives an error message with `error: "Invalid message format"`. If the message is completely unparseable, gp-api receives nothing — check CloudWatch logs for the error.

**No response at all:** If gp-api sends a message and never receives a completion or error message, possible causes are:
- The Lambda crashed before it could send anything (check CloudWatch logs)
- The SQS message is still being retried (check the input queue's "messages in flight")
- The message landed in the DLQ after exhausting retries (Slack alert should have fired)

**Error message values gp-api may receive in the `error` field:**
- `"Campaign plan generation failed"` — Gemini API or Google Search failed after all retries
- `"Invalid message format"` — the input message was malformed

## Architecture

- **Input queue:** `campaign-plan-input-{env}.fifo` (FIFO, batch size 1)
- **Dead letter queue:** `campaign-plan-dlq-{env}.fifo` (after 3 failed attempts)
- **S3 bucket:** `campaign-plan-results-{env}` (12-month retention)
- **Output queue:** gp-api's existing `{env}-Queue.fifo` (shared with other services)
- **Alerts:** DLQ alarm → SNS → shared Slack notifier
- **Runtime:** Python 3.12, 1GB memory, 15-minute timeout
- **AI:** Gemini 3 Flash with Google Search grounding (2 API calls per invocation)
