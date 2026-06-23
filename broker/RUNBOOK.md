# PMF Broker Runbook

Operational procedures for the PMF Engine v2 broker service.

**AWS Account:** 333022194791 | **Region:** us-west-2
**ECR:** `333022194791.dkr.ecr.us-west-2.amazonaws.com/gp-ai-projects`
**Tags:** `broker-dev`, `broker-qa`, `broker-prod`

---

## 1. Deploying a Broker Update

### Via CI (preferred)

Push to `develop`/`qa`/`prod` with changes under `broker/`. The `build-broker.yml` workflow builds, pushes to ECR, and force-redeploys the ECS service.

### Manual

```bash
export ENV=dev
export AWS_PROFILE=work
export ACCOUNT=333022194791
export REGION=us-west-2
export ECR=${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com/gp-ai-projects

docker build --platform linux/arm64 \
  -f broker/Dockerfile \
  -t ${ECR}:broker-${ENV} .

aws ecr get-login-password --region ${REGION} | \
  docker login --username AWS --password-stdin ${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com

docker push ${ECR}:broker-${ENV}

aws ecs update-service \
  --cluster broker-${ENV} \
  --service broker-${ENV} \
  --force-new-deployment \
  --region ${REGION}
```

Monitor rollout:

```bash
aws ecs wait services-stable \
  --cluster broker-${ENV} \
  --services broker-${ENV} \
  --region ${REGION}
```

---

## 2. Deploying an Agent Image Update

Agent images use a separate Dockerfile (`pmf_engine/Dockerfile`) and tag (`pmf-engine-{env}`). The broker does NOT need redeployment when the agent image changes.

```bash
export ENV=dev

docker build --platform linux/arm64 \
  -f pmf_engine/Dockerfile \
  -t ${ECR}:pmf-engine-${ENV} .

docker push ${ECR}:pmf-engine-${ENV}
```

New agent tasks automatically pull the latest image with the same tag. No ECS service update is needed since agents are ephemeral Fargate tasks launched per-run.

---

## 3.1 Initial Secret Population (first deploy)

After Terraform creates `broker-{env}` and `broker-service-tokens-{env}` they are empty (both use `ignore_changes = [secret_string]`). Populate them before the broker can do work.

Prerequisites: `AI_SECRETS_{ENV_UPPER}` already holds `PMF_ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `BRAINTRUST_API_KEY`, `DATABRICKS_SERVER_HOSTNAME`, `DATABRICKS_HTTP_PATH`, `DATABRICKS_API_KEY`.

> **WARNING:** `BRAINTRUST_API_KEY` MUST be present in the `broker-{env}` secret BEFORE running `terraform apply` on the broker module. The ECS task definition injects it via a Secrets Manager `valueFrom` entry, and ECS fails the entire task at launch with `ResourceInitializationError` if the JSON key is absent — the broker will not start in any env whose secret lacks the key. Dev was already backfilled; qa and prod MUST be backfilled before promoting.

**Ordering matters.** Put the new hash into the broker secret *first*, redeploy broker to pick it up, *then* put the new plaintext into the service-tokens secret, *then* bounce the dispatch Lambda. Reversing the order leaves a window where the Lambda presents the new token to a broker still holding the old hash → `BrokerServiceTokenAuthFailure` alarm fires.

```bash
export ENV=dev
export AWS_PROFILE=work
export REGION=us-west-2

AI=$(aws secretsmanager get-secret-value --secret-id AI_SECRETS_${ENV^^} --region $REGION --query SecretString --output text)
TOKEN=$(uuidgen)
HASH=$(printf '%s' "$TOKEN" | shasum -a 256 | awk '{print $1}')

# 1. Put broker secret (contains the hash + API keys).
aws secretsmanager put-secret-value \
  --secret-id broker-${ENV} \
  --region $REGION \
  --secret-string "$(jq -n \
    --arg a "$(jq -r .PMF_ANTHROPIC_API_KEY   <<<"$AI")" \
    --arg g "$(jq -r .GEMINI_API_KEY          <<<"$AI")" \
    --arg bt "$(jq -r .BRAINTRUST_API_KEY     <<<"$AI")" \
    --arg h "$(jq -r .DATABRICKS_SERVER_HOSTNAME <<<"$AI")" \
    --arg p "$(jq -r .DATABRICKS_HTTP_PATH    <<<"$AI")" \
    --arg k "$(jq -r .DATABRICKS_API_KEY      <<<"$AI")" \
    --arg s "$HASH" \
    '{ANTHROPIC_API_KEY:$a, GEMINI_API_KEY:$g, BRAINTRUST_API_KEY:$bt, DATABRICKS_SERVER_HOSTNAME:$h, DATABRICKS_HTTP_PATH:$p, DATABRICKS_API_KEY:$k, SERVICE_TOKEN_HASH:$s}')"

# 2. Force-redeploy broker so it reads the new hash. Wait stable.
aws ecs update-service \
  --cluster broker-${ENV} \
  --service broker-${ENV} \
  --force-new-deployment \
  --region $REGION
aws ecs wait services-stable \
  --cluster broker-${ENV} \
  --services broker-${ENV} \
  --region $REGION

# 3. Put the plaintext token into the service-tokens secret.
aws secretsmanager put-secret-value \
  --secret-id broker-service-tokens-${ENV} \
  --region $REGION \
  --secret-string "{\"SERVICE_TOKEN\":\"$TOKEN\"}"

# 4. Bounce dispatch Lambda so it re-resolves SERVICE_TOKEN at cold start.
#    Terraform reads the secret at apply; for a plain rotation, a config update
#    (empty description change) is enough to force a new version.
aws lambda update-function-configuration \
  --function-name pmf-engine-dispatch-${ENV} \
  --description "SERVICE_TOKEN rotation $(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --region $REGION >/dev/null
```

Notes:
- `TOKEN` is a UUIDv4 used by the dispatch Lambda as Bearer auth to `POST /internal/mint-run-token`.
- `HASH` is what the broker checks. They must be generated together; never regenerate one without the other.
- The dispatch Lambda reads `SERVICE_TOKEN` from `broker-service-tokens-{env}` at cold start. Step 4 forces a cold start by updating config; without it, warm containers keep the old token until the function idles out.

---

## 3. Rotating Secrets

### API Keys (Anthropic, Gemini, Databricks)

```bash
export ENV=dev
export AWS_PROFILE=work

aws secretsmanager get-secret-value \
  --secret-id broker-${ENV} \
  --query SecretString --output text | jq .

aws secretsmanager put-secret-value \
  --secret-id broker-${ENV} \
  --secret-string "$(jq -n \
    --arg anthropic "NEW_KEY" \
    --arg gemini "NEW_KEY" \
    --arg braintrust "NEW_KEY" \
    --arg db_host "HOSTNAME" \
    --arg db_path "HTTP_PATH" \
    --arg db_key "NEW_KEY" \
    --arg sth "EXISTING_HASH" \
    '{
      ANTHROPIC_API_KEY: $anthropic,
      GEMINI_API_KEY: $gemini,
      BRAINTRUST_API_KEY: $braintrust,
      DATABRICKS_SERVER_HOSTNAME: $db_host,
      DATABRICKS_HTTP_PATH: $db_path,
      DATABRICKS_API_KEY: $db_key,
      SERVICE_TOKEN_HASH: $sth
    }')"

aws ecs update-service \
  --cluster broker-${ENV} \
  --service broker-${ENV} \
  --force-new-deployment \
  --region us-west-2
```

The broker reads secrets at boot via ECS container secrets injection. A force-redeploy picks up new values.

### Service Token (used by dispatch Lambda)

1. Generate a new token: `python3 -c "import uuid; print(uuid.uuid4())"`
2. Hash it: `python3 -c "import hashlib; print(hashlib.sha256(b'TOKEN_HERE').hexdigest())"`
3. Update `broker-{env}` secret with the new `SERVICE_TOKEN_HASH`
4. Update `broker-service-tokens-{env}` secret with the new plaintext `SERVICE_TOKEN`
5. Force-redeploy the broker (picks up new hash)
6. Update the dispatch Lambda env (picks up new token from secrets resolve)

---

## 4. Adding a New Service Caller

A service caller is any component that calls `POST /internal/mint-run-token` on the broker.

1. Generate a service token (UUIDv4)
2. Hash it with SHA-256
3. Add the hash to the broker's `SERVICE_TOKEN_HASH` in `broker-{env}` secrets (if supporting multiple callers, the broker auth module needs to accept a list)
4. Store the plaintext token in the caller's secrets
5. The caller sends `Authorization: Bearer <token>` to the broker's `/internal/mint-run-token` endpoint

---

## 5. Debugging a Failed Run

### Trace path: run_id -> broker logs -> DynamoDB -> agent logs

```bash
export ENV=dev
export RUN_ID=<the_run_id>
export AWS_PROFILE=work

# 1. Check broker logs for the run
aws logs filter-log-events \
  --log-group-name /ecs/broker-${ENV} \
  --filter-pattern "\"${RUN_ID}\"" \
  --start-time $(date -v-1d +%s000) \
  --region us-west-2 \
  --query 'events[].message' --output text

# 2. Check DynamoDB scope ticket
aws dynamodb get-item \
  --table-name broker-scope-tickets-${ENV} \
  --key "{\"pk\": {\"S\": \"${RUN_ID}\"}}" \
  --region us-west-2

# 3. Check agent logs
aws logs filter-log-events \
  --log-group-name /ecs/pmf-engine-${ENV} \
  --filter-pattern "\"${RUN_ID}\"" \
  --start-time $(date -v-1d +%s000) \
  --region us-west-2 \
  --query 'events[].message' --output text

# 4. Check results queue DLQ for unprocessed results
aws sqs get-queue-attributes \
  --queue-url "https://sqs.us-west-2.amazonaws.com/333022194791/broker-agent-results-dlq-${ENV}.fifo" \
  --attribute-names ApproximateNumberOfMessages \
  --region us-west-2

# 5. Check S3 for artifacts
aws s3 ls "s3://gp-agent-artifacts-${ENV}/" --recursive | grep "${RUN_ID}"
```

### Common failure patterns

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| 401 in broker logs | Expired or invalid run token | Check DynamoDB TTL; verify dispatch Lambda is minting tokens |
| 403 on /internal/mint-run-token | Bad SERVICE_TOKEN | Rotate per section 3 |
| Classifier timeout in broker logs | Gemini API slow/down | Check Gemini status; classifier falls back to structural checks |
| Agent task exits with code 1, no broker logs | Agent cannot reach broker | Check SG rules; verify Service Connect namespace |
| DynamoDB ConditionalCheckFailedException | Duplicate run_id (race) | Idempotency issue in dispatch; check for duplicate SQS messages |

---

## 6. Rolling Back

### Broker rollback

```bash
export ENV=dev
export PREVIOUS_TAG=broker-${ENV}

# Find the previous image digest
aws ecr describe-images \
  --repository-name gp-ai-projects \
  --image-ids imageTag=${PREVIOUS_TAG} \
  --region us-west-2

# Re-tag a known-good image (from a previous commit SHA if available)
# Or revert the code change and push

# Force redeploy to pick up the reverted image
aws ecs update-service \
  --cluster broker-${ENV} \
  --service broker-${ENV} \
  --force-new-deployment \
  --region us-west-2
```

### Agent rollback

Same pattern with `pmf-engine-{env}` tag. New agent tasks automatically use the reverted image.

### Terraform rollback

```bash
cd infrastructure
git revert <commit>
terraform plan -var-file=envs/${ENV}.tfvars
terraform apply -var-file=envs/${ENV}.tfvars
```

---

## 7. Adding a New URL to Experiment Allowlist

The broker DNS firewall controls which domains the broker can resolve. To allow the broker to reach a new external API:

1. Edit `infrastructure/modules/broker/main.tf`
2. Add the domain to the `aws_route53_resolver_firewall_domain_list.broker_allow` resource
3. Apply Terraform

```hcl
resource "aws_route53_resolver_firewall_domain_list" "broker_allow" {
  domains = [
    # ... existing domains ...
    "new-api.example.com.",  # <-- add trailing dot
  ]
}
```

Agent tasks do NOT need DNS changes -- they can only reach the broker hostname (`broker-{env}.ai.goodparty.org`) and all external access goes through the broker.

---

## 8. Handling Classifier Outage

The classifier uses Gemini Flash to screen fetched web content for prompt injection. If Gemini is down:

### Symptoms
- `BrokerClassifierException` CloudWatch alarm fires
- Agent runs stall or fail on research/fetch steps
- Broker logs show Gemini timeouts or 5xx

### Response

1. **Check Gemini status:** https://status.cloud.google.com/
2. **If transient:** The classifier has a fail-closed design. During an outage, research/fetch requests are rejected (not silently passed through). Runs will fail but no data is at risk.
3. **If prolonged (>30 min):** Consider pausing experiment dispatch:
   ```bash
   aws lambda update-event-source-mapping \
     --uuid <EVENT_SOURCE_MAPPING_UUID> \
     --no-enabled \
     --region us-west-2
   ```
4. **Recovery:** Re-enable the event source mapping. In-flight runs that failed will need to be re-dispatched from gp-api.

The classifier does NOT fall open. A compromised classifier that returns false negatives is the accepted residual risk documented in PLAN_V2.md.

---

## 9. Responding to Injection Alert

Triggered when `BrokerRunTokenAuthFailure` or `ParamScreeningRejected` alarms fire, or when reviewing broker logs shows classifier rejections.

### Immediate steps

1. **Identify the run_id** from the alarm/logs
2. **Pull the full broker log** for that run (see section 5)
3. **Check what content was flagged:**
   ```bash
   aws logs filter-log-events \
     --log-group-name /ecs/broker-${ENV} \
     --filter-pattern "\"classifier\" \"REJECT\"" \
     --start-time $(date -v-1H +%s000) \
     --region us-west-2 \
     --query 'events[].message' --output text
   ```
4. **If the rejection was a false positive:** No action needed; the run failed cleanly. The candidate can retry.
5. **If the rejection caught a real injection:**
   - Note the source URL that contained the injection
   - Check if other runs fetched the same URL
   - Consider adding the domain to a blocklist (not yet implemented; log an issue)
   - No data was exfiltrated (agent has no credentials or egress)

### Escalation

If you see patterns of repeated injection attempts targeting a specific experiment or data source, escalate to the team. The containment guarantee holds: even successful injections cannot exfiltrate data. But repeated attempts may indicate a targeted attack worth investigating.
