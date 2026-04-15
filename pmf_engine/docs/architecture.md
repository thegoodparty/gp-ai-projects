# PMF Engine Architecture

## What It Is

A platform for shipping AI-powered product features. Each feature is defined as an experiment — a markdown instruction, a contract schema, and resource config — that runs as an autonomous Claude agent in its own runtime. The platform handles dispatch, execution, validation, and delivery across three services (gp-api, gp-ai-projects, gp-webapp).

## How It Works

### 1. Design the Experiment

An experiment starts with three things:

- **Instruction** — a markdown playbook that tells the agent what to do, step by step. What data to gather, how to process it, what output to produce.
- **Contract schema** — a JSON schema defining the exact shape of the output artifact. This is both the validation layer and the spec for the eventual deterministic replacement.
- **Resource config** — model (Opus/Sonnet), agent harness, turn budget, CPU/memory, mode (win/serve).

These are registered in a central registry that both the dispatch Lambda and the agent runner import from. Adding a new experiment is: write the playbook, define the schema, register it.

### 2. Trigger

A run can be triggered by anything — a user clicking a button, a backend event, a scheduled job, or a preload on signup. Whatever the trigger, it hits gp-api, which validates eligibility, auto-populates params from the available data, creates a run record in the database (PENDING), and dispatches a message onto an SQS FIFO queue.

### 3. Input Screening + Dispatch

The dispatch queue triggers a Lambda that validates the incoming message — checks that the experiment exists in the registry, the params meet the experiment's requirements, the request is well-formed, and inputs are screened for prompt injection and data poisoning. If screening passes, it launches a runtime (Fargate ECS task) with the experiment config passed as container overrides. Malformed, invalid, or unsafe requests are rejected before any compute spins up.

### 4. Agent Execution

Inside the runtime, the Claude Agent SDK loads the instruction, contract schema, and params. The agent runs autonomously for up to the turn budget, with access to tools: Bash, Read, Write, Edit, Glob, Grep, WebFetch. What data sources it queries and what processing it performs depends entirely on the experiment's instruction — the platform is agnostic.

The agent writes its output to a known path. The runner validates the output against the contract schema. If it passes, the artifact uploads to S3. If it violates the schema, no upload — the run is marked CONTRACT_VIOLATION and considered a failure.

### 5. Callback

The runner sends a callback message to an SQS queue. A callback Lambda validates the artifact exists in S3 and forwards the result to gp-api's consumer queue. The queue consumer updates the run record (SUCCESS, FAILED, or CONTRACT_VIOLATION) with the artifact location, duration, and cost.

### 6. Delivery

The frontend polls until the run status changes, then fetches the artifact through gp-api (which retrieves it from S3) and renders experiment-specific results UI.

---

## Why It's Built This Way

**De-risks product bets.** Instead of spending weeks building a feature based on assumptions, we let an agent figure out what works, watch what users actually use, and only then invest engineering time. If an experiment gets no adoption, we kill it cheap. If it takes off, we already have the spec (the contract schema) and a reference implementation (the execution traces).

**Unit economics improve over time by design.** Most AI features get more expensive as they scale. Ours get cheaper — because the successful ones graduate to deterministic code. The Opus costs are R&D, not COGS.

**Plays to our constraints.** Small team, lots of product surface area, unclear which features will land. This lets us explore broadly without committing engineering resources to every idea.

**The discipline requirement.** This only works if we actually follow through on hardening. The risk is running agents forever because the experiments "work fine." The meta-strategy only pays off if successful experiments graduate to code and stop costing per-run.

---

## Rollout

Experiments are gated behind `isAiBetaVip` — only beta customers have access. This keeps the blast radius small while we validate the platform, collect execution data, and learn which experiments are worth hardening. As experiments prove out, we widen access.

---

## First Experiments

### WIN Mode (candidates running for office)

| Experiment | Turns | Output |
|---|---|---|
| **Voter Targeting** | 50 | Tiered voter segments with demographics, Haystaq scores, voter IDs |
| **Walking Plan** | 60 | Door-knocking routes with geographic clusters, Maps URLs, optimal walk order |

### SERVE Mode (elected officials)

| Experiment | Turns | Output | Dependencies |
|---|---|---|---|
| **District Intel** | 60 | Active local issues with citations, affected constituents, demographics | — |
| **Peer City Benchmarking** | 60 | Peer city policy comparisons | Requires district_intel |
| **Meeting Briefing** | 100 | Agenda items, fiscal data, voting recommendation scores | Optional: district_intel |

When District Intel is regenerated, downstream experiments (Peer City, Meeting Briefing) are marked **STALE**.

---

## Data Sources

The platform is data-source agnostic — each experiment's instruction defines what to query. Current experiments use:

### Structured Data
- **L2 + Haystaq** (Databricks) — nationwide voter file with demographics, addresses, lat/lng, party, voting history, Haystaq propensity scores
- **Legistar API** — municipal government data: meetings, agendas, votes, council members

### Fiscal APIs
- **NC LINC** (linc.osbm.nc.gov) — property tax rates, revenues/expenditures, population
- **Ohio Checkbook** + Ohio Dept of Taxation
- **Texas Comptroller** — property tax database
- Other states: web search fallback

### Web Research
- **Tavily Search API** — news, policy research, peer city discovery
- **Municipal websites** — council minutes, budgets, policy pages (scraped)
- **Local news** — articles, candidate profiles

### Alternative Meeting Platforms
When a city doesn't use Legistar: eSCRIBE, CivicPlus AgendaCenter, BoardDocs

### Other
- **Google Maps** — walking direction URLs for canvassing routes
- **S3 artifacts** — cross-experiment dependencies (e.g. district_intel consumed by peer_city_benchmarking)

---

## Key Design Decisions

**Registry pattern** — single source of truth for experiment config. Both Lambda dispatch and Fargate runner import from it. Adding a new experiment = markdown playbook + JSON schema + registry entry.

**Contract validation** — JSON schema checked before S3 upload. On violation, no upload. Schema also injected into the agent's system prompt so it knows the expected output format. Does double duty: runtime safety net + prompt engineering.

**Auto-populated params** — gp-api builds experiment params from the user's data so they just click one button. No configuration required.

**Dependency chaining** — experiments can depend on other experiments' artifacts. Regenerating an upstream experiment marks downstream runs as STALE.

---

## Infrastructure

| Component | Resource |
|---|---|
| Docker images | ECR: gp-ai-projects (tags: pmf-engine-{env}) |
| Compute | ECS Fargate cluster: pmf-engine-{env} |
| Artifacts | S3: gp-agent-artifacts-{env} |
| Queues | SQS FIFO: agent-dispatch, agent-callback, agent-results (each with DLQ) |
| Control plane | Lambda: pmf-engine-dispatch-{env}, pmf-engine-callback-{env} |
| Database | ExperimentRun table in gp-api Aurora PG |
| IaC | Terraform: infrastructure/modules/pmf-engine-control-plane/ |

---

## Three Repos

| Repo | Role |
|---|---|
| **gp-ai-projects** | Experiment definitions, agent runner, control plane Lambdas, infrastructure |
| **gp-api** | Orchestration: validate, dispatch, consume results, serve artifacts |
| **gp-webapp** | UI: trigger experiments, poll status, render results |

---

## The Meta-Strategy

The agents are temporary. Every experiment running on Opus today is meant to be replaced by deterministic code. The platform exists to figure out what that code should do.

1. **Explore** — ship a new AI-powered feature fast by writing a runbook and a contract schema. The agent figures out the execution: what data to query, what logic to apply, what output to produce.
2. **Collect** — every run generates execution traces. We capture the queries the agent wrote, the processing steps it took, the data sources it hit, and what the final output looked like.
3. **Measure** — track adoption. Which experiments get used, regenerated, exported? Which outputs do users act on? This tells us what to harden first.
4. **Harden** — once we see the pattern across enough runs, extract the 80% path into deterministic code. No agent, no LLM costs. The experiment becomes a standard feature.

The end state for any successful experiment is that it no longer needs an agent at all.
