# PMF Broker Architecture

Companion to `architecture.md` — describes the broker-centric design in detail. The high-level PMF Engine overview belongs in `architecture.md`; this file documents the broker: what it is, the trust boundary it enforces, how it's deployed, and how runners reach it.

---

## Purpose

The broker is the **single trusted egress** for every PMF agent. It holds all API credentials, proxies external service calls, validates artifacts before they land, and signs the callback that gp-api consumes. The runner task is an **untrusted quarantine** — a Fargate task with empty IAM, narrow network egress, and no ambient credentials.

The design target (from the prior v2 redesign): a compromised agent — via prompt injection in fetched web content — cannot exfiltrate data or influence any other run. At worst, a compromised agent produces a malformed artifact that fails validation.

---

## Trust boundary

```
  ┌────────────────────────────── VPC vpc-0763fa52c32ebcf6a ──────────────────────────────┐
  │                                                                                       │
  │   Dispatch Lambda (in VPC)      Broker ECS service (long-lived)     VPC Endpoints     │
  │   ─────────────────────         ───────────────────────────         ──────────────    │
  │   sg: dispatch-lambda-sg        sg: broker-sg                   sg: pmf-vpce-sg   │
  │     egress  tcp/443 → vpce-sg     ingress  tcp/8080 from agent-sg     (ENIs for ECR   │
  │     egress  tcp/443 → 0.0.0.0/0   ingress  tcp/8080 from dispatch-sg   api/dkr, Logs) │
  │     egress  udp/53  → VPC CIDR    egress   tcp/443  → 0.0.0.0/0                       │
  │     egress  tcp/8080 → broker-sg  egress   udp/53   → VPC CIDR                        │
  │                                                                                       │
  │                       Runner Fargate task (ephemeral, per run)                        │
  │                       ───────────────────────────────────────                         │
  │                       sg: pmf-engine-ecs-tasks-dev                                    │
  │                         egress  tcp/8080 → broker-sg   (broker calls)                 │
  │                         egress  tcp/443  → vpce-sg     (ECR + Logs)                   │
  │                         egress  tcp/443  → S3 prefix   (ECR layer blobs)              │
  │                         egress  udp/53   → VPC CIDR    (VPC DNS)                      │
  │                       task IAM role: EMPTY (trust policy only)                        │
  │                       task secrets block: EMPTY (no Secrets Manager fetch at init)    │
  │                       AWS metadata endpoint disabled via entrypoint                   │
  └───────────────────────────────────────────────────────────────────────────────────────┘
```

The broker is the **only** TCP destination the runner can reach for application traffic. ECR and CloudWatch Logs traffic goes via VPC endpoints (narrow allowlist). Nothing else.

---

## Deployed components

### Terraform stacks

| Stack | What it owns | Scope |
|---|---|---|
| `environments/dev/pmf-vpc-endpoints` | ECR api/dkr + CloudWatch Logs interface endpoints, S3 gateway endpoint, `pmf-vpce-sg`. | **Shared across all PMF envs** in the same VPC — one ~$44/mo bill covers dev/qa/prod. |
| `environments/dev/broker` | Broker ECS cluster/service/task-def, public ACM cert + Route53 ALIAS at `broker-{env}.ai.goodparty.org`, DynamoDB scope tickets table, `broker-*` + `broker-service-tokens-*` secrets, broker task role (DynamoDB + S3 + SQS + Logs), broker SG, alarms. | Per env. |
| `environments/dev/pmf-engine-fargate` | Runner ECS cluster + task definition, empty task role, quarantined agent SG with 3 narrow egress rules, SNS failure topic wired to Slack. | Per env. |
| `environments/dev/pmf-engine-control-plane` | Dispatch Lambda (VPC-attached), dispatch + results SQS queues, S3 artifacts bucket, dispatch Lambda SG. | Per env. |

### Runtime resources (dev)

| Resource | Identifier |
|---|---|
| Broker ECS cluster | `broker-dev` (desired_count = 1) |
| Runner ECS cluster | `pmf-engine-dev` (launched on-demand by dispatch Lambda) |
| Broker hostname (dev) | `broker-dev.ai.goodparty.org` (Route53 public ALIAS → internal ALB; ACM public cert) |
| DynamoDB | `broker-scope-tickets-dev` (per-run auth tickets, TTL-based) |
| SQS dispatch | `agent-dispatch-dev.fifo` + DLQ |
| SQS results | `agent-results-dev.fifo` + DLQ |
| S3 artifacts | `gp-agent-artifacts-dev` |
| Secrets | `broker-dev` (API keys + SERVICE_TOKEN_HASH); `broker-service-tokens-dev` (plaintext SERVICE_TOKEN for Lambda) |
| Images (ECR) | `gp-ai-projects:broker-dev`, `gp-ai-projects:pmf-engine-dev` |

---

## Key design decisions

### 1. Runner quarantine via SG egress (not dedicated VPC)

The agent SG (`pmf-engine-ecs-tasks-dev`) permits only four outbound destinations:

- `tcp/8080 → broker-sg` (broker mint + artifact publish + DB proxy)
- `tcp/443 → pmf-vpce-sg` (ECR image pull auth/manifest + CloudWatch Logs)
- `tcp/443 → S3 prefix list `pl-68a54001`` (ECR image **layer blobs** — gateway endpoint routes packets through VPC route tables, but the SG still inspects destination IP and S3's IPs are public, so the prefix list is the narrow allowlist)
- `udp/53 → VPC CIDR` (VPC DNS resolver)

No NAT path, no broad 443 egress, no internet gateway. Even if an agent is prompt-injected, TCP connections to anywhere except those four destinations drop at the SG. Combined with empty task IAM and disabled metadata endpoint, the runner has no credentials to use even if it could reach AWS APIs.

**Why not a dedicated PMF VPC?** A dedicated VPC would let us attach Route53 DNS Firewall (block-all except the broker hostname) as a belt-and-suspenders DNS layer. SG-based containment already holds — DNS can return anything, SG drops the connect. Dedicated VPC is documented as a future hardening pass (Phase 3).

### 2. Public DNS ALIAS → internal ALB (HTTPS)

Broker is fronted by an internal ALB with an ACM public cert for `broker-{env}.ai.goodparty.org`. Route53 public zone `goodparty.org` has an ALIAS A record pointing at the internal ALB. The ALB is internal — it has no public IP, so the DNS resolving externally is information-only; only clients inside the VPC can actually connect.

**Why not a private hosted zone (split-horizon)?** A private zone scoped to `ai.goodparty.org` would shadow any future public `*.ai.goodparty.org` names from inside the VPC, which is a surprise cost. A private zone scoped exactly to the broker hostname works but is unusual. Public DNS + internal ALB is the standard AWS pattern and the IP leak is mild (private 10.x address of an unreachable ALB). ACM validation CNAMEs also live in the public zone.

**Why HTTPS on an internal ALB?** Scope tickets carry authority to query voter-data tables; plaintext HTTP 8080 inside the VPC would leak them on any ENI mirror, compromised neighbor service, or misconfigured flow-log sink. Public ACM certs are free and auto-renewing.

**Forward path — per-task broker pools with ALB routing.** Today the ALB has one target group fronting a single broker service (scaled by CPU/memory). As experiment shapes diverge (e.g., Anthropic-proxy-heavy briefing runs vs. Databricks-heavy data-extraction runs vs. future low-cost PDF parsers), we want to split the broker into pools with different resource profiles and route traffic by run_type. Two extension points are already in place:

1. **ALB listener rules.** The HTTPS listener can host path-based or header-based rules (`condition { path_pattern }` or `condition { http_header }`). Example: `/databricks/*` → `broker-db-{env}` target group with more memory; `/anthropic/*` → `broker-proxy-{env}` target group with higher concurrency. Hostname stays the same, clients don't change — the ALB dispatches.
2. **Header-routed pools.** Mint-time we can stamp a `X-Broker-Pool` header on the scope ticket and have the runner/dispatch include it on every request; ALB rules route on that header. Lets us A/B a new broker version or isolate a noisy experiment without a separate hostname.

Per-pool autoscaling then falls out naturally — each target group is a separate ECS service with its own `aws_appautoscaling_policy`. Cost: one ALB, multiple target groups (free). The DNS firewall allow-list stays at one entry. This is a future change, not today's code; documenting the intent so the current single-pool setup doesn't ossify.

### 3. VPC endpoints are shared, scoped by egress

The `pmf-vpce-sg` allows 443 ingress from VPC CIDR — any service in the shared VPC that resolves `ecr.us-west-2.amazonaws.com` (DNS answer points at the endpoint ENI via `private_dns_enabled = true`) can use them. Other VPC tenants (gp-api, people-api, election-api) transparently benefit.

PMF's quarantine is enforced by the **narrow egress side** on the agent SG (443 only to vpce-sg + S3 prefix list), not by the endpoint SG. Permissive endpoint ingress does not weaken PMF's containment.

Cost consequence: one bill of ~$44/mo (3 interface endpoints × 2 AZ = ~$44, S3 gateway is free) covers all PMF envs living in the same VPC.

### 4. Dispatch Lambda in VPC

The Lambda has a `vpc_config` attaching it to the VPC with its own SG (`pmf-dispatch-lambda-sg`). Required so it can resolve the broker hostname and reach the ALB on 443. Cold start first invocation takes 2-3s (ENI attach); subsequent invocations are fast. The Lambda's SG has 443 to the world for AWS SDK calls via NAT.

### 5. ANTHROPIC_BASE_URL points at broker's `/anthropic` prefix

Broker mounts the Anthropic proxy router at `/anthropic`. Dispatch Lambda sets `ANTHROPIC_BASE_URL = https://broker-{env}.ai.goodparty.org/anthropic` as a container override on the runner task. Claude Agent SDK transparently routes its `/v1/messages` calls under that base URL. Anthropic API key lives only in the broker's env (from `broker-{env}` secret).

### 6. WebSearch allowed via Anthropic proxy; URL fetches go through broker `/http/fetch`

Runner's `ALLOWED_TOOLS` includes `WebSearch` (Claude SDK built-in). WebSearch is server-side at Anthropic — search queries piggyback on the `/messages` API call (already broker-proxied through `/anthropic/v1/messages`), and results return inline. The runner never makes a separate outbound request for search.

`WebFetch` is **not** in `ALLOWED_TOOLS`. Claude SDK runs WebFetch client-side in the runner container and requires direct egress to `claude.ai` for URL safety pre-checks — egress the runner SG explicitly denies. All URL retrieval now goes through `pmf_runtime.http.get(url)` and `pmf_runtime.pdf.download(url)`, which call broker `/http/fetch` and `/pdf/fetch` respectively. The broker is the sole egress path; every URL fetch is auditable.

Broker no longer exposes `research_fetch` or `research_search` endpoints.

---

## Broker HTTP interface

All routes require `X-Broker-Token` (per-run UUIDv4) except `/health` and `/internal/mint-run-token` (which uses `Authorization: Bearer <SERVICE_TOKEN>` from dispatch Lambda).

| Path | Used by | Purpose |
|---|---|---|
| `GET /health` | ECS health check | liveness only |
| `POST /internal/mint-run-token` | Dispatch Lambda | Validates SERVICE_TOKEN, creates a scope ticket in DynamoDB, returns a per-run broker token (UUIDv4) the runner uses for auth |
| `POST /anthropic/v1/messages` | Runner (via Claude SDK) | Proxies to `api.anthropic.com`, injects broker's `ANTHROPIC_API_KEY`; streams response back |
| `POST /databricks/query` | Runner (via `pmf_runtime.databricks`) | Scope-aware SQL execution — broker rewrites WHERE clauses to inject `state`/`city` filters per scope ticket; rejects cross-scope queries |
| `POST /artifact/publish` | Runner (via `pmf_runtime.publish`) | Validates artifact against contract schema, uploads to S3, sends callback to results queue |
| `GET /artifact/read` | Runner (via `pmf_runtime.priors`) | Reads a prior experiment's artifact (for dependency chaining, e.g. one experiment reading a prior experiment's artifact — the dependency relation is encoded in gp-api, not the broker) |
| `POST /run-status` | Runner | Update run status (RUNNING/CONTRACT_VIOLATION/etc.); broker sends callback to results queue |
| `POST /internal/upload-logs` | Runner | Forward runner logs to S3 for debugging |

Full spec: see `broker/endpoints/*.py`.

---

## Runner → broker auth flow

```
  1. gp-api → SQS dispatch queue (message with run_id, experiment_id, params)

  2. Dispatch Lambda (from SQS trigger):
     - Reads SERVICE_TOKEN from broker-service-tokens-{env} Secrets Manager secret
     - POST /internal/mint-run-token (Bearer SERVICE_TOKEN)
       → broker writes scope ticket to DynamoDB (TTL + run_id + organization_slug + scope)
       → returns broker_token (UUIDv4)
     - Calls ecs:RunTask with container overrides:
         BROKER_URL=https://broker-{env}.ai.goodparty.org
         ANTHROPIC_BASE_URL=https://broker-{env}.ai.goodparty.org/anthropic
         BROKER_TOKEN=<uuid>
         ANTHROPIC_API_KEY=<uuid>  (same token; SDK uses it as auth header)
         RUN_ID, EXPERIMENT_ID, ORGANIZATION_SLUG, PARAMS_JSON, ...

  3. Runner Fargate task:
     - Pulls image via ECR endpoints + S3 prefix list
     - Starts Claude Agent SDK (passes ANTHROPIC_BASE_URL + ANTHROPIC_API_KEY as env)
     - Every API call: Claude SDK → broker /anthropic/v1/messages (token auth)
     - Every DB query: pmf_runtime.databricks → broker /databricks/query
     - Final output: pmf_runtime.publish → broker /artifact/publish

  4. Broker /artifact/publish:
     - Fetches scope ticket by broker_token
     - Validates artifact JSON against experiment contract schema
     - Uploads to s3://gp-agent-artifacts-{env}/{experiment_id}/{run_id}/artifact.json
     - Sends callback to agent-results-{env}.fifo (gp-api consumes)

  5. Scope ticket expires via DynamoDB TTL (default 1 hour); subsequent calls
     with the same token return 401.
```

---

## Security layers

| Layer | What it prevents |
|---|---|
| Narrow runner SG egress | Agent cannot reach the internet, any AWS service except via vpce-sg, or any other VPC tenant |
| Empty task IAM role | Even if network were open, no AWS credentials to use |
| Disabled metadata endpoint | SDKs can't fetch ECS task role credentials from `169.254.170.2` |
| Per-run scope ticket (broker_token) | Broker rejects cross-run API calls — a stolen token is useless after TTL and useless for another run |
| Scope-aware SQL rewriter (broker) | Runner's Databricks queries auto-scoped to its own candidate's state/city; cross-scope access denied |
| Contract validation (broker) | Malformed artifact rejected; never lands in S3 or results queue |
| Broker task IAM (limited) | Broker can write only to the specific S3 prefix + SQS queue; cannot invoke Lambdas or read other secrets |
| TLS (broker → upstream APIs) | In transit to Anthropic / Databricks / Tavily (via NAT) |

Note: runner → broker traffic inside the VPC is HTTP (no TLS). Not a leak — `awsvpc` network mode gives each task its own ENI, no shared wire with other tenants, and no traffic mirroring enabled in the VPC.

---

## Image management

- **Broker image** (`broker/Dockerfile`): FastAPI + uvicorn + sqlglot + httpx + boto3. Pushed as `gp-ai-projects:broker-dev`. Rebuilt + pushed manually today; CI wiring is a future task.
- **Runner image** (`pmf_engine/Dockerfile`): Python 3.12 + Claude CLI (pinned) + Node 22 + AWS CLI + `pdftotext` (poppler-utils) + `pmf_runtime` shim. Playwright/chromium stripped — runner has no internet egress, browser automation would be useless.
- **Lambda package** (`pmf_engine/.lambda_build/`): flat dispatch_handler + broker_client + scope_derivation + dispatch_registry + vendored httpx (via `pip install --target --platform manylinux2014_aarch64`). Built via `pmf_engine/scripts/build_lambda_package.sh`.

---

## Operational references

- **Secret rotation + initial population**: `broker/RUNBOOK.md` sections 3, 3.1
- **Debugging a failed run**: `broker/RUNBOOK.md` section 5
- **ECS Exec** into broker for live debug: `enable_execute_command = true` on the service; connect with `aws ecs execute-command --cluster broker-{env} --task <task> --container broker --command "sh" --interactive`
- **SSM Session Manager plugin** required on local machine for ECS Exec
- **Slack alerts**: broker + runner failure topics both publish to `pmf-engine-failures-{env}` SNS, subscribed by `shared-slack-notifier` Lambda

---

## Open items for QA/prod rollout

1. **Image tagging for CI.** Current manual `docker build && docker push` is fine for dev iteration. QA/prod need CI-driven tagging (e.g. `broker-{env}-{commit_sha}` with service redeployment).
2. **Braintrust tracing.** Stripped from runner (required IAM the quarantine doesn't allow). If we want per-run Claude API traces visible in Braintrust, route via broker — broker's Anthropic proxy can forward headers / tag requests.
3. **gp-api integration.** `agent-results-{env}.fifo` is currently unconsumed. gp-api consumer wiring + experiment endpoint schemas are tracked separately.
4. **Dedicated PMF VPC (Phase 3 hardening).** Move PMF to its own VPC so Route53 DNS Firewall can attach with a block-all-except-broker rule. Not blocking for dev/qa/prod MVP.
5. **SSRF hardening (batch 1b).** `broker/endpoints/http_fetch.py` and `pdf_fetch.py` do SSRF URL validation via `getaddrinfo` then let httpx re-resolve — DNS rebinding window. Pending: resolve once, pin to safe IP via custom httpx resolver. Also: IPv4-mapped IPv6 (`::ffff:169.254.169.254`) bypasses the private-range check because Python's `is_private` returns False for v6-mapped; need explicit `ip.ipv4_mapped` handling.
6. **Secret split.** Broker secret currently holds all 6 API keys + `SERVICE_TOKEN_HASH` in one ARN. Splitting `SERVICE_TOKEN_HASH` into its own secret narrows blast radius if the broker container ever leaks its env.
