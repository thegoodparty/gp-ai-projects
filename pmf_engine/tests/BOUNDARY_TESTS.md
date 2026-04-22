# PMF Engine Failure Boundary Tests

Every boundary in the system, every condition to test at that boundary.
Status: `[x]` = has test, `[ ]` = needs test.

---

## A. gp-webapp → gp-api (HTTP Request)

| # | Condition | Status |
|---|-----------|--------|
| A1 | Valid experimentId dispatches successfully | [x] |
| A2 | Invalid experimentId rejected by Zod enum | [x] |
| A3 | Missing experimentId in request body | [x] |
| A4 | Missing JWT token → 401 | [x] |
| A5 | User without candidate role → 403 | [x] |

## B. gp-api: Campaign Lookup & Gating

| # | Condition | Status |
|---|-----------|--------|
| B1 | User has no campaign → 404 | [x] |
| B2 | Campaign not VIP (isAiBetaVip=false) → 403 | [x] |
| B3 | campaign.details is null → 400 | [x] |
| B4 | campaign.details is a string (not object) → 400 | [x] |

## C. gp-api: Experiment Mode Routing

| # | Condition | Status |
|---|-----------|--------|
| C1 | Win experiment dispatches with correct autoParams | [x] |
| C2 | Serve experiment dispatches with correct autoParams | [x] |
| C3 | Win experiment missing pathToVictory → 400 | [x] |
| C4 | Win experiment missing electionType → 400 | [x] |
| C5 | Serve experiment no elected office → 403 | [x] |
| C6 | Serve experiment missing state → 400 | [x] |
| C7 | Server autoParams cannot be overridden by caller params | [x] |
| C8 | Unknown experimentId defaults to win mode (verify or reject) | [x] |

## D. gp-api: Concurrent Run Prevention

| # | Condition | Status |
|---|-----------|--------|
| D1 | PENDING run exists for same experiment+candidate → 400 | [x] |
| D2 | RUNNING run exists for same experiment+candidate → 400 | [x] |
| D3 | SUCCESS run exists → allows new dispatch | [x] |
| D4 | FAILED run exists → allows new dispatch | [x] |

## E. gp-api: Dependency Resolution

| # | Condition | Status |
|---|-----------|--------|
| E1 | peer_city_benchmarking: no SUCCESS district_intel → 400 | [x] |
| E2 | peer_city_benchmarking: SUCCESS district_intel → passes artifact ref | [x] |
| E3 | meeting_briefing: no district_intel → dispatches without it (optional) | [x] |
| E4 | meeting_briefing: SUCCESS district_intel → passes artifact ref | [x] |
| E5 | district_intel dispatch → marks peer_city + meeting_briefing as STALE | [x] |

## F. gp-api: SQS Dispatch

| # | Condition | Status |
|---|-----------|--------|
| F1 | DB create + SQS send both succeed → PENDING run | [x] |
| F2 | SQS send fails → marks run FAILED, throws 502 | [x] |
| F3 | DB create fails → SQS never attempted | [x] |
| F4 | SQS message body contains correct fields (experimentId, organizationSlug, runId, params) | [x] |
| F5 | SQS message uses correct groupId and dedup key | [x] |

## G. Lambda Dispatch Handler (SQS → ECS)

| # | Condition | Status |
|---|-----------|--------|
| G1 | Valid message → ECS RunTask succeeds | [x] |
| G2 | Invalid JSON body → batch failure | [x] |
| G3 | Missing experiment_id → batch failure | [x] |
| G4 | Missing organization_slug → batch failure | [x] |
| G5 | Missing run_id → batch failure | [x] |
| G6 | Unknown experiment_id → error callback sent | [x] |
| G7 | ECS RunTask returns failures array → error callback + batch failure | [x] |
| G8 | ECS RunTask returns empty tasks array → error callback + batch failure | [x] |
| G9 | ECS RunTask throws exception → batch failure (no callback) | [x] |
| G10 | Container overrides include all required env vars | [x] |
| G11 | Container overrides include TIMEOUT_SECONDS | [x] |
| G12 | Experiment missing contract.s3_key_template → KeyError | [x] |
| G13 | send_error_callback SQS send fails → logged, not thrown | [x] |

## H. Fargate Runner (main.py)

| # | Condition | Status |
|---|-----------|--------|
| H1 | Happy path: harness → contract valid → S3 upload → success callback | [x] |
| H2 | Harness throws exception → failed callback | [x] |
| H3 | S3 upload fails → failed callback | [x] |
| H4 | Contract violation → contract_violation callback, no S3 upload | [x] |
| H5 | Experiment exceeds timeout_seconds → failed callback + exit(1) | [x] |
| H6 | SIGTERM received → failed callback + exit(1) | [x] |
| H7 | Instruction written to /workspace/instruction.md before run | [x] |
| H8 | Callback SQS send_message fails → what happens? (currently silent) | [x] |
| H9 | Agent produces no output file → FileNotFoundError | [x] |
| H10 | Agent produces multiple output files → RuntimeError | [x] |
| H11 | Missing EXPERIMENT_ID env var → exit(1) | [x] |
| H12 | Missing instruction (not in env or registry) → exit(1) | [x] |

## I. Claude SDK Harness

| # | Condition | Status |
|---|-----------|--------|
| I1 | Agent completes successfully → HarnessResult with cost/turns | [x] |
| I2 | Agent returns error (is_error=true) → RuntimeError | [x] |
| I3 | Agent stream ends without ResultMessage → RuntimeError | [x] |
| I4 | Params included in system prompt as JSON | [x] |
| I5 | Contract schema included in system prompt | [x] |
| I6 | Instruction reference (/workspace/instruction.md) in prompt | [x] |
| I7 | max_turns passed to SDK options | [x] |

## J. Output Artifact Collection

| # | Condition | Status |
|---|-----------|--------|
| J1 | Single JSON file → correct content_type | [x] |
| J2 | Single PDF file → correct content_type | [x] |
| J3 | Single CSV file → correct content_type | [x] |
| J4 | Unknown extension → application/octet-stream | [x] |
| J5 | Multiple files → RuntimeError | [x] |
| J6 | Empty output dir → FileNotFoundError | [x] |
| J7 | Missing output dir → FileNotFoundError | [x] |

## K. Contract Validation

| # | Condition | Status |
|---|-----------|--------|
| K1 | Valid artifact passes | [x] |
| K2 | Invalid JSON → ContractViolation | [x] |
| K3 | Not a JSON object (array) → ContractViolation | [x] |
| K4 | Missing top-level field → ContractViolation | [x] |
| K5 | Wrong type for field → ContractViolation | [x] |
| K6 | Empty required array → ContractViolation | [x] |
| K7 | Nested object missing field → ContractViolation | [x] |
| K8 | Array item missing field → ContractViolation | [x] |
| K9 | None/empty schema → skip validation | [x] |
| K10 | Extra fields allowed (not rejected) | [x] |
| K11 | Per-experiment schema: voter_targeting valid artifact | [x] |
| K12 | Per-experiment schema: walking_plan valid artifact | [x] |
| K13 | Per-experiment schema: district_intel valid/invalid | [x] |
| K14 | Per-experiment schema: peer_city_benchmarking valid/invalid | [x] |
| K15 | Per-experiment schema: meeting_briefing valid/invalid | [x] |

## L. Lambda Callback Handler (Fargate → gp-api)

| # | Condition | Status |
|---|-----------|--------|
| L1 | Success status + valid S3 artifact → forward to results queue | [x] |
| L2 | Success status + S3 artifact missing (404) → rewrite to contract_violation | [x] |
| L3 | Success status + S3 non-404 error → batch failure | [x] |
| L4 | Success status + artifact_bucket mismatch → batch failure | [x] |
| L5 | Failed status → forward without S3 check | [x] |
| L6 | contract_violation status → forward (already rewritten) | [x] |
| L7 | Invalid JSON body → batch failure | [x] |
| L8 | Missing required field → batch failure | [x] |
| L9 | SQS forward to results queue fails → batch failure | [x] |
| L10 | Empty artifact_key/bucket → validate_contract returns False | [x] |
| L11 | Message envelope uses camelCase keys | [x] |
| L12 | Message group ID is correct (gp-queue-agentExperiments) | [x] |

## M. gp-api: Queue Consumer (agentExperimentResult)

| # | Condition | Status |
|---|-----------|--------|
| M1 | success → SUCCESS status + artifact fields saved | [x] |
| M2 | failed → FAILED status + error saved | [x] |
| M3 | contract_violation → CONTRACT_VIOLATION status | [x] |
| M4 | Run not found for runId → log error, ack message | [x] |
| M5 | Run already SUCCESS → skip (idempotency) | [x] |
| M6 | Run already FAILED → skip (idempotency) | [x] |
| M7 | Run is STALE → skip (terminal guard) | [x] |
| M8 | Zod rejects unknown status value | [x] |
| M9 | Zod rejects missing experimentId | [x] |
| M10 | Zod rejects missing runId | [x] |
| M11 | Zod rejects missing organizationSlug | [x] |
| M12 | DB update fails during status transition | [x] |
| M13 | durationSeconds stored correctly | [x] |

## N. gp-api: Artifact Retrieval

| # | Condition | Status |
|---|-----------|--------|
| N1 | Valid run → returns parsed JSON artifact | [x] |
| N2 | Run not found → 404 | [x] |
| N3 | Run belongs to different campaign → 403 | [x] |
| N4 | artifactBucket is null → 404 | [x] |
| N5 | artifactKey is null → 404 | [x] |
| N6 | S3 returns null → 404 | [x] |
| N7 | S3 returns invalid JSON → 400 | [x] |
| N8 | S3 throws (bucket doesn't exist) | [x] |

## O. gp-api: Stale Run Sweeper

| # | Condition | Status |
|---|-----------|--------|
| O1 | PENDING runs older than 30min → marked FAILED | [x] |
| O2 | RUNNING runs older than 30min → marked FAILED | [x] |
| O3 | No stale runs → no-op, no log | [x] |
| O4 | SUCCESS runs not affected | [x] |
| O5 | Runs younger than 30min not affected | [x] |

## P. Config Loading

| # | Condition | Status |
|---|-----------|--------|
| P1 | All fields loaded from env vars | [x] |
| P2 | Default values when env empty | [x] |
| P3 | Invalid PARAMS_JSON → empty dict | [x] |
| P4 | TIMEOUT_SECONDS loaded from env | [x] |
| P5 | TIMEOUT_SECONDS loaded from registry | [x] |
| P6 | TIMEOUT_SECONDS non-integer string → ValueError | [x] |
| P7 | Experiment config loaded from registry when INSTRUCTION not in env | [x] |

## Q. Dispatch Registry ↔ Full Registry Sync

| # | Condition | Status |
|---|-----------|--------|
| Q1 | All experiments in DISPATCH_REGISTRY match EXPERIMENT_REGISTRY | [x] |
| Q2 | s3_key_templates match between registries | [x] |
| Q3 | All experiments have timeout_seconds | [x] |
| Q4 | timeout_seconds values match between registries | [x] |

## R. Cross-Service Contract (callback Lambda → gp-api queue consumer)

| # | Condition | Status |
|---|-----------|--------|
| R1 | Envelope type field is "agentExperimentResult" | [x] |
| R2 | Data field names are camelCase (experimentId, runId, not experiment_id) | [x] |
| R3 | Status values match Zod enum (success, failed, contract_violation) | [x] |
| R4 | Optional fields (artifactKey, artifactBucket, durationSeconds, error) pass Zod | [x] |

---

## Summary

| Section | Total | Tested |
|---------|-------|--------|
| A. HTTP Request | 5 | 5 |
| B. Campaign Gating | 4 | 4 |
| C. Mode Routing | 8 | 8 |
| D. Concurrent Prevention | 4 | 4 |
| E. Dependencies | 5 | 5 |
| F. SQS Dispatch | 5 | 5 |
| G. Lambda Dispatch | 13 | 13 |
| H. Fargate Runner | 12 | 12 |
| I. Claude SDK Harness | 7 | 7 |
| J. Output Collection | 7 | 7 |
| K. Contract Validation | 15 | 15 |
| L. Lambda Callback | 12 | 12 |
| M. Queue Consumer | 13 | 13 |
| N. Artifact Retrieval | 8 | 8 |
| O. Stale Run Sweeper | 5 | 5 |
| P. Config Loading | 7 | 7 |
| Q. Registry Sync | 4 | 4 |
| R. Cross-Service Contract | 4 | 4 |
| **Total** | **142** | **142** |
