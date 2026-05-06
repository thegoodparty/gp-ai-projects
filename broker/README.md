# Broker

Agent-traffic proxy. Sits between autonomous agents (Claude Agent SDK running on Fargate) and the outside world — Anthropic, Databricks, S3, arbitrary HTTPS. Carries:

- Scope tickets (per-run auth, TTL-bound, stored in DynamoDB)
- A SQL rewriter that enforces voter-data scoping by injecting `WHERE` clauses per ticket
- Content sanitization (HTML, PII) and output contract validation at publish time
- SSRF-safe URL fetches (rejects private IPs / scheme / redirect hops)

The broker is **agnostic to the consumer's domain**. It sees two identifiers in a scope ticket:

- `experiment_id` — consumer's run-type-id. e.g. PMF engine experiments use the `experiment_id` from their manifest; a different consumer (engineer_agent, future services) would use whatever run-type namespace fits.
- `organization_slug` — consumer's scope-owner-id. PMF engine uses an organization slug; a different consumer might use a workspace ID, a user ID, a ticket ID. The broker stores it as an opaque string for S3 key partitioning and access scoping.

Neither field carries meaning to the broker. Consumers are free to use any string space; the broker only enforces uniqueness-within-ticket and URL-safety of the characters.

## Consumers

- `pmf_engine/control_plane/broker_client.py` — PMF engine's dispatch Lambda mints run tokens via `POST /internal/mint-run-token`.
- `pmf_engine/runner/pmf_runtime/` — PMF engine's Fargate runner calls `/artifact/publish`, `/artifact/read`, `/anthropic`, `/databricks/query`, `/http/fetch`, `/pdf/fetch`, `/run-status`, `/internal/upload-logs`.
- Future: `engineer_agent` (currently calls Anthropic directly; migration planned).

## HTTP interface

See `ARCHITECTURE.md` for the full endpoint list, auth flow, and deployment topology. See `RUNBOOK.md` for ops procedures (rotation, DNS allowlist edits, alarm thresholds).

## Topology: one hostname, ALB as the routing layer

Clients always talk to **one hostname** per environment: `broker-dev.ai.goodparty.org` (dev), `broker-qa.ai.goodparty.org` (qa), `broker.ai.goodparty.org` (prod). Behind that hostname sits an internal ALB that terminates TLS (public ACM cert) and forwards to a target group of broker tasks.

Today the target group points at a single broker ECS service. Tomorrow, when one broker shape isn't enough, **the ALB is the seam for splitting into pools** — no consumer-side change, no DNS churn, no new hostname.

Extension points already in place:

1. **Path-based routing.** Add ALB listener rules with `condition { path_pattern }`. Example: route `/databricks/*` to `broker-db-dev` (more memory, smaller concurrency), `/anthropic/*` to `broker-proxy-dev` (higher concurrency, less memory), everything else to the default pool. Each path gets its own target group + its own ECS service + its own autoscaling.
2. **Header-based routing.** Stamp an `X-Broker-Pool` header at mint time (store on the scope ticket, return to the runner, include on every call). ALB rules route on the header. Lets you A/B a broker version or isolate a noisy experiment without touching paths or hostnames.
3. **Per-pool autoscaling.** Each target group is a separate ECS service with its own `aws_appautoscaling_policy`. CPU-bound vs memory-bound workloads scale independently.

**Contract:** clients MUST use the hostname, not the ALB's raw DNS name or a task IP. The hostname is the stable address; everything behind it is free to reshape. `BROKER_URL` in the dispatch Lambda and the runner task def is always the hostname; there is no shortcut.

The current single-pool setup is the simplest valid configuration of this topology, not a different design. When load or cost shapes warrant, the split happens in `infrastructure/modules/broker/` (add target groups + listener rules + ECS services); nothing in the consumer code changes.

## Deploy

- `.github/workflows/build-broker.yml` builds + pushes to ECR on merge to `develop` / `qa` / `prod`.
- `infrastructure/modules/broker/` is the Terraform module.
- `infrastructure/environments/{env}/broker/` is the per-env wrapper.

## Development

```bash
cd ~/work/gp-ai-projects
uv sync                         # workspace install, picks up broker/
uv run pytest broker/tests/     # ~20 test modules
```

No `shared/` import; no `pmf_engine/` import. The broker has zero cross-package Python dependencies.
