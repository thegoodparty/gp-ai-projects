# Braintrust Integration

Braintrust provides LLM observability, evaluation, and prompt management for gp-ai-projects.

## Enablement Strategy

**API key presence controls enablement:**
- `BRAINTRUST_API_KEY` present → Braintrust enabled
- `BRAINTRUST_API_KEY` absent → gracefully disabled (no-op, no errors)

This follows the principle of least privilege: services only get credentials they need.

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `BRAINTRUST_API_KEY` | No | None | Enables Braintrust when set |

## Project Naming

Each folder in gp-ai-projects maps to a Braintrust project. Pass the project name in code:

| Folder | Braintrust Project |
|--------|-------------------|
| `serve/hierarchical_discovery` | `hierarchical-discovery` |
| `ai_generated_campaign_plan` | `campaign-plan` |
| `serve/analyze_texts` | `analyze-texts` |

## Usage

```python
from shared.braintrust import (
    init_braintrust,
    traced_llm_call,
    load_prompt_from_braintrust,
    flush_logs,
    is_enabled,
)

# Initialize at startup with project name (required)
init_braintrust(project="hierarchical-discovery")

# Wrap LLM calls for tracing
result = traced_llm_call(
    name="cluster_analysis",
    input_data={"cluster_id": 5, "messages": ["..."]},
    llm_call_fn=lambda: client.generate_content(prompt),
    metadata={"source": "hierarchical_discovery"}
)

# Load prompts from Braintrust (with local fallback)
prompt = load_prompt_from_braintrust(
    prompt_name="cluster-analysis-v1",
    fallback_prompt="Analyze this: {input}",
    variables={"input": data}
)

# Flush logs before shutdown
flush_logs()

# Check if enabled
if is_enabled():
    print("Braintrust is active")
```

## How to Enable for a New Service

### 1. Add Secret to AWS Secrets Manager

The API key should already be in `AI_SECRETS_DEV`, `AI_SECRETS_QA`, and `AI_SECRETS_PROD`. If not:

```bash
# Get current secret value
aws secretsmanager get-secret-value --secret-id AI_SECRETS_DEV --query SecretString --output text | jq '.'

# Update with new key (merge with existing)
aws secretsmanager update-secret --secret-id AI_SECRETS_DEV --secret-string '{"GEMINI_API_KEY":"...", "BRAINTRUST_API_KEY":"sk-xxx", ...}'
```

### 2. Add Secret Reference in Terraform

In your service's Terraform module (e.g., `infrastructure/modules/serve-analyze-fargate/main.tf`), add to `container_definitions` secrets:

```hcl
secrets = [
  # ... existing secrets ...
  {
    name      = "BRAINTRUST_API_KEY"
    valueFrom = "arn:aws:secretsmanager:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:secret:AI_SECRETS_${upper(var.environment)}:BRAINTRUST_API_KEY::"
  }
]
```

### 3. Initialize in Code

In your service code, call `init_braintrust` with the project name:

```python
from shared.braintrust import init_braintrust

init_braintrust(project="your-project-name")
```

### 4. Deploy

```bash
cd infrastructure/environments/dev/serve-analyze-fargate
terraform apply
```

## Rollout History

| Service | Environment | Status | Date |
|---------|-------------|--------|------|
| serve-analyze (hierarchical_discovery) | dev | Experiment | TBD |

## Architecture Notes

- **Singleton pattern**: One BraintrustClient instance per process
- **Graceful degradation**: If braintrust package not installed or API key missing, all functions become no-ops
- **No double LLM calls**: The `traced_llm_call` function executes the LLM call once, then logs. If logging fails, the result is still returned.

## Troubleshooting

**Braintrust not logging:**
1. Check if `BRAINTRUST_API_KEY` is set: `echo $BRAINTRUST_API_KEY`
2. Check logs for "Braintrust initialized" or "BRAINTRUST_API_KEY not set"
3. Verify `braintrust` package is installed: `uv add braintrust`
