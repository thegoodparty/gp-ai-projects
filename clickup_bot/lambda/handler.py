import hashlib
import hmac
import json
import os
import time
from typing import Any
from urllib.request import Request, urlopen

import boto3

CLICKUP_BASE_URL = "https://api.clickup.com/api/v2"
_secrets_cache = None


def get_secrets() -> dict:
    global _secrets_cache
    if _secrets_cache is not None:
        return _secrets_cache

    environment = os.environ.get("ENVIRONMENT", "prod").upper()
    secret_id = f"AI_SECRETS_{environment}"

    client = boto3.client("secretsmanager")
    response = client.get_secret_value(SecretId=secret_id)
    _secrets_cache = json.loads(response["SecretString"])
    print(f"Loaded secrets from {secret_id}")
    return _secrets_cache


def get_clickup_api_key() -> str:
    return get_secrets().get("CLICKUP_API_KEY", "")


def get_webhook_secret() -> str:
    return get_secrets().get("CLICKUP_WEBHOOK_SECRET", "")


def verify_webhook_signature(body: str, signature: str) -> bool:
    secret = get_webhook_secret()
    if not secret:
        print("ERROR: No CLICKUP_WEBHOOK_SECRET configured, rejecting request")
        return False

    expected = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    try:
        is_valid = hmac.compare_digest(expected, signature)
    except TypeError:
        # compare_digest raises TypeError on non-ASCII str input. The header is
        # attacker-controlled, so a malformed signature is an invalid signature
        # (401) — it must never propagate to the handler's secrets-outage 500.
        print("ERROR: Webhook signature verification failed: malformed signature header")
        return False

    if not is_valid:
        # ERROR prefix is load-bearing: a rotated/mismatched CLICKUP_WEBHOOK_SECRET
        # 401s every gpbot delivery until ClickUp suspends the webhook, and this
        # log line is what fires the CloudWatch alarm (see the metric filter in
        # infrastructure/modules/clickup-bot/main.tf).
        print("ERROR: Webhook signature verification failed: signature mismatch")

    return is_valid


BOT_PREFIX = "[GP-Bot]"
PROCESSING_STARTED_PREFIX = f"{BOT_PREFIX} Processing started"

# How long a "Processing started" marker blocks re-triggering. The marker
# exists to absorb webhook retry storms and double-tags (seconds-to-minutes
# timescale — ClickUp retried one delivery 5x within ~45s in the 2026-07-14
# incident). A human re-tagging a task hours later is a deliberate re-run and
# must NOT be silently ignored: dedup had never actually fired before that
# incident's fix, so "re-tag always re-runs" is the observed behavior users
# know, and an unbounded marker would silently change it.
DEFAULT_DEDUP_COMMENT_WINDOW_SECONDS = 900.0

# Pause before retrying the "Processing started" ack post once. Applies ONLY
# to the async worker (retry_ack=True): there ClickUp already has its 200, so
# a brief wait is free and rides out transient ClickUp 5xxs/timeouts. The
# synchronous fallback never sleeps or retries — it runs while ClickUp is
# still waiting on the webhook response. Tests zero this out — the length is
# not a behavioral contract.
ACK_COMMENT_RETRY_DELAY_SECONDS = 2.0


def get_dedup_window_seconds() -> float:
    raw = os.environ.get("DEDUP_COMMENT_WINDOW_SECONDS")
    if raw:
        try:
            return float(raw)
        except ValueError:
            # A typo'd env var must not crash deliveries (this runs in-path,
            # post-auth) and must not spam the alarm on every dedup check —
            # fall back to the safe default with a quiet, non-alarm log line
            # (no "ERROR"/"Failed to": see the metric-filter contract below).
            print("Invalid DEDUP_COMMENT_WINDOW_SECONDS env value; using default 900s")
    return DEFAULT_DEDUP_COMMENT_WINDOW_SECONDS


# Repo guidance (omni monorepo, archived standalone repos) deliberately does
# NOT live here: it is baked into the agent's capability prompt
# (engineer_agent/agent/config.py, pinned by engineer_agent/tests). These
# instructions carry only the per-task contract.
ANALYZE_INSTRUCTION = """## YOUR TASK: Analyze and Report

**Approach this ticket with healthy skepticism.** It may be out of date - the issue
could have been fixed, the data may have changed, the description may be incomplete,
or the reporter may have been incorrect.

## VERIFICATION (required)

- Verify every claim against the live code in omni and cite `file:line`.
- Attempt to reproduce the issue before concluding anything about it.
- State explicitly when something could not be verified.

Post your analysis to ClickUp when done. Be concise.
"""

IMPLEMENT_INSTRUCTION = """## YOUR TASK: Implement and Create PR

**Approach this ticket with healthy skepticism.** It may be out of date - the issue
could have been fixed, the data may have changed, the description may be incomplete,
or the reporter may have been incorrect.

**BEFORE writing any code**, you MUST:
1. Find all files that use/import the function or component you plan to modify
2. Read each of those files to understand how they depend on it
3. Consider if your change will break any of those usages

## RED/GREEN TDD (required)

Write a **failing test** that reproduces the bug or defines the feature contract
FIRST, confirm it fails, then implement the minimum to make it pass. No production
code without a driving test. Run the affected package's test suite and lint before
opening the PR.

## SELF-REVIEW (required, after the diff is green)

Perform two self-review passes over the full diff using the rule files in the
repo's `ai-rules/` directory (bugs, breaking-changes, security, test quality),
fixing what they surface. The PR description MUST include the failing (red) test
output and the passing (green) run.

If you're unsure about the solution or the impact is too broad, post a comment explaining
your findings and recommend a human handle the implementation.

Branch naming: `<custom_id>/gp-bot_<description-slug>` (use the task's custom_id like ENG-1234, not the internal ID)
PR title format: `[GP-Bot] <custom_id> <description>`
PRs target omni's `develop` branch.

Post the PR link to ClickUp when done.
"""

TAG_CONFIG = {
    "gpbot-analyze": {"instruction": ANALYZE_INSTRUCTION, "label": "analyze", "model": "opus"},
    "gpbot-work": {"instruction": IMPLEMENT_INSTRUCTION, "label": "implement", "model": "opus"},
}


def clickup_request(method: str, endpoint: str, data: dict | None = None) -> dict:
    url = f"{CLICKUP_BASE_URL}{endpoint}"
    headers = {
        "Authorization": get_clickup_api_key(),
        "Content-Type": "application/json",
    }

    body = json.dumps(data).encode() if data else None
    req = Request(url, data=body, headers=headers, method=method)

    with urlopen(req, timeout=10) as response:
        return json.loads(response.read().decode())


def get_task_comments(task_id: str) -> list[dict]:
    result = clickup_request("GET", f"/task/{task_id}/comment")
    return result.get("comments", [])


def has_processing_started_comment(comments: list[dict], label: str, now: float | None = None) -> bool:
    # Only the success marker counts as 'already processed'. Failure comments
    # ('[GP-Bot] Failed to start processing: ...') must NOT block a retry:
    # removing and re-adding the tag after a failure has to re-trigger.
    #
    # LABEL SCOPE: dedup is per (task, label), mirroring the atomic layer's
    # {task_id}#{label} DynamoDB key. The ack text is
    # '{PROCESSING_STARTED_PREFIX} ({label}, model: ...)', so matching the
    # label-scoped prefix lets analyze and implement dedup independently — a
    # fresh gpbot-analyze marker must not suppress a gpbot-work trigger
    # (analyze-then-implement inside the window is the normal workflow).
    #
    # SHAPE CONTRACT (2026-07-14 incident): the real GET /task/{id}/comment
    # response carries the full text in a top-level "comment_text" field, and
    # its comment[] items have NO "type" key. The previous matcher required
    # item["type"] == "text", so it matched 0 real comments — including the
    # bot's own ack comments — and dedup never fired once in prod: one webhook
    # delivery retried 6x launched 6 Fargate agents. Prefer comment_text; fall
    # back to concatenating item["text"] WITHOUT filtering on "type" (tolerate
    # its presence for forward-compat if ClickUp ever ships one).
    #
    # RECENCY: a marker only blocks while younger than the dedup window (see
    # DEFAULT_DEDUP_COMMENT_WINDOW_SECONDS for why). The real API's "date" is
    # a STRING of epoch milliseconds; a missing/unparseable date is treated as
    # recent (block) — conservative against retry storms. `now` is injectable
    # so tests can pin exact boundaries.
    if now is None:
        now = time.time()
    window_seconds = get_dedup_window_seconds()
    label_scoped_prefix = f"{PROCESSING_STARTED_PREFIX} ({label}"
    for comment in comments:
        comment_text = comment.get("comment_text")
        if comment_text is None:
            comment_text = "".join(
                item.get("text", "") for item in comment.get("comment", []) if isinstance(item, dict)
            )
        if not comment_text.startswith(label_scoped_prefix):
            continue
        try:
            age_seconds = now - int(comment.get("date")) / 1000.0
        except (TypeError, ValueError):
            return True
        if age_seconds <= window_seconds:
            return True
    return False


def post_comment(task_id: str, text: str) -> None:
    clickup_request(
        "POST",
        f"/task/{task_id}/comment",
        {
            "comment_text": text,
            "notify_all": False,
        },
    )


def post_failure_comment(task_id: str, error_msg: str) -> None:
    # A failed comment post must never change the handler's control flow.
    try:
        post_comment(
            task_id,
            f"{BOT_PREFIX} Failed to start processing: {error_msg}. Remove and re-add the tag to retry.",
        )
    except Exception as comment_err:
        print(f"Failed to post failure comment to ClickUp: {comment_err}")


def redact_signature(event: dict) -> dict:
    # The x-signature header is the webhook's shared-secret HMAC; a copy of it
    # in CloudWatch is a replayable credential. Return the event with only that
    # header value replaced, leaving everything else intact for debugging.
    # Case-insensitive: API Gateway / ClickUp may deliver any header casing.
    # ALB also carries the same value under multiValueHeaders (values are lists),
    # so redact that copy too — otherwise the debug log line leaks the secret.
    redacted = {**event}
    headers = event.get("headers", {})
    redacted["headers"] = {
        key: ("[redacted]" if key.lower() == "x-signature" else value) for key, value in headers.items()
    }
    multi_value_headers = event.get("multiValueHeaders")
    if isinstance(multi_value_headers, dict):
        redacted["multiValueHeaders"] = {
            key: (["[redacted]"] if key.lower() == "x-signature" else value)
            for key, value in multi_value_headers.items()
        }
    return redacted


def get_header_case_insensitive(headers: dict, name: str, default: str = "") -> str:
    name_lower = name.lower()
    for key, value in headers.items():
        if key.lower() == name_lower:
            return value
    return default


def find_matched_tag(history_items: Any) -> str | None:
    # Runs BEFORE signature verification, so the shape is attacker-controlled:
    # null/non-list history_items, non-dict entries, and non-dict tags must
    # skip quietly instead of crashing into a runtime "[ERROR]" log that would
    # fire the fail-loud alarm (see the log-poisoning guard in handler()).
    if not isinstance(history_items, list):
        return None
    for item in history_items:
        if not isinstance(item, dict):
            continue
        if item.get("field") == "tag" and item.get("after"):
            after_tags = item["after"]
            if isinstance(after_tags, list):
                for tag in after_tags:
                    if not isinstance(tag, dict):
                        continue
                    tag_name = (tag.get("name") or "").lower()
                    if tag_name in TAG_CONFIG:
                        return tag_name
    return None


def enqueue_async_processing(task_id: str, matched_tag: str) -> bool:
    # FAST-ACK (2026-07-14 incident): ClickUp's webhook delivery has a short
    # response timeout. When the handler did dedup GET + run_task + ack POST
    # in-path (7.6-20.5s during a ClickUp slowdown), every delivery timed out:
    # ClickUp retried one taskTagUpdated event 6x (6 Fargate launches) AND
    # counted each timeout toward the ~100 consecutive failures after which it
    # auto-disables the webhook — slow responses are an outage risk, not just
    # a duplication risk. So the handler hands the work to an async self-invoke
    # and answers ClickUp in milliseconds; the worker invocation does the
    # ClickUp/ECS work off the critical path.
    #
    # False = "process synchronously instead". The self-invoke IAM permission
    # ships in a separate terraform PR, so AccessDenied here is the initial
    # prod state: the fallback must be quiet (no "ERROR"/"Failed to" — see the
    # alarm metric-filter contract in handler()) and must preserve exactly the
    # old synchronous behavior.
    function_name = os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
    if not function_name:
        print("Async self-invoke unavailable, processing synchronously: AWS_LAMBDA_FUNCTION_NAME not set")
        return False
    try:
        boto3.client("lambda").invoke(
            FunctionName=function_name,
            InvocationType="Event",
            Payload=json.dumps({"gpbot_async": True, "task_id": task_id, "matched_tag": matched_tag}),
        )
        return True
    except Exception as e:
        print(f"Async self-invoke unavailable, processing synchronously: {e}")
        return False


def is_atomic_dedup_configured() -> bool:
    # Branch-point predicate for the comment-fetch failure handling below:
    # whether the atomic DynamoDB dedup layer (conditional PutItem keyed
    # {task_id}#{label}) is available as a backstop. The atomic layer itself
    # ships in the stacked atomic-dedup PR, so in THIS PR the env var is never
    # set in prod and the configured arm is dead code — the branch point is
    # defined and tested now so that PR only has to slot its conditional-write
    # check into the proceed path.
    return bool(os.environ.get("DEDUP_TABLE_NAME"))


def dedup_check_then_trigger(task_id: str, matched_tag: str, from_async_worker: bool = False) -> dict:
    # Shared by the async worker and the synchronous fallback so the two paths
    # cannot drift: whichever path runs, the dedup semantics and the trigger
    # behavior are identical. from_async_worker is the one deliberate
    # divergence: it gates behavior that is only safe once ClickUp already has
    # its 200 (ack retry with a pause; see trigger_fargate_task) and behavior
    # that only makes sense when nobody receives the HTTP response (the
    # comment-fetch failure handling below).
    try:
        comments = get_task_comments(task_id)
    except Exception as e:
        # HTTPError is only raised for HTTP status errors; connection-phase
        # failures (URLError, TimeoutError, RemoteDisconnected) must also land
        # here, never crash the Lambda. Alarm-matching ("Failed to") in every
        # arm: a broken comments GET degrades or blocks dedup either way.
        print(f"Failed to get comments for task {task_id}: {e}")
        if not from_async_worker:
            # SYNC: ClickUp receives this 500 and redelivers — self-healing
            # at-least-once. Nothing more to do.
            return {"statusCode": 500, "body": json.dumps({"error": "failed to get comments"})}
        # ASYNC: ClickUp already got its 200 'accepted', so this return value
        # goes NOWHERE — a bare 500 dict would permanently drop the tag event
        # with zero feedback on the ticket.
        if is_atomic_dedup_configured():
            # The comment check is best-effort; the atomic conditional write
            # still guards duplicates. Dropping verified work is worse than
            # skipping a best-effort check: proceed with empty comments.
            comments = []
        else:
            # No atomic backstop: launching blind is unbounded duplicate risk,
            # so this is deliberately AT-MOST-ONCE — stop, and give the tagger
            # a visible retry path (the standard failure comment ends in
            # 'Remove and re-add the tag to retry.') instead of silence.
            # Exception TYPE only in the public comment (leak guard, same as
            # trigger_fargate_task); full detail is already in the logs above.
            post_failure_comment(task_id, f"{type(e).__name__} fetching ClickUp comments (see CloudWatch logs)")
            return {"statusCode": 500, "body": json.dumps({"error": "failed to get comments"})}

    config = TAG_CONFIG[matched_tag]
    if has_processing_started_comment(comments, config["label"]):
        print(f"Task {task_id} already has a recent {PROCESSING_STARTED_PREFIX} ({config['label']}) comment, skipping")
        return {"statusCode": 200, "body": json.dumps({"skipped": "already processed"})}

    return trigger_fargate_task(
        task_id, config["instruction"], config["label"], config["model"], retry_ack=from_async_worker
    )


def handle_async_processing(event: dict) -> dict:
    # Worker half of the fast-ack design: this invocation was enqueued by
    # enqueue_async_processing AFTER signature verification, task_id validation
    # and tag resolution, so the payload is trusted (see the dispatch guard in
    # handler() for why it cannot be spoofed through the ALB).
    task_id = None
    try:
        task_id = event.get("task_id")
        matched_tag = event.get("matched_tag")
        # Defensive re-validation: the payload is self-generated, so a miss
        # here means a bug (or a direct invoke by something with AWS creds) —
        # refuse loudly, never launch. ERROR prefix fires the alarm.
        if not task_id or matched_tag not in TAG_CONFIG:
            print("ERROR: Async processing failed: invalid internal payload (missing task_id or unknown matched_tag)")
            return {"statusCode": 400, "body": json.dumps({"error": "invalid async payload"})}
        return dedup_check_then_trigger(task_id, matched_tag, from_async_worker=True)
    except Exception as e:
        # The worker must NEVER raise: an unhandled exception in an async
        # ("Event") invocation makes Lambda auto-RETRY it (2x by default),
        # which would re-create exactly the duplicate-launch bug this design
        # fixes. And since nobody receives an HTTP error from an async
        # invocation, this alarm-matching ERROR log is the only fail-loud
        # channel — plus a best-effort failure comment for the tagger.
        print(f"ERROR: Async processing failed: {e}")
        if task_id:
            # Same leak guard as trigger_fargate_task: exception type only in
            # the public comment, full detail stays in CloudWatch.
            post_failure_comment(task_id, f"{type(e).__name__} (see CloudWatch logs for details)")
        return {"statusCode": 500, "body": json.dumps({"error": "async processing failed"})}


def handler(event: dict, context: Any) -> dict:
    # INTERNAL ASYNC DISPATCH: the fast-ack path re-invokes this same function
    # asynchronously with {"gpbot_async": true, ...}. Only dispatch to the
    # trusted worker path when the marker is top-level AND the event carries
    # no ALB envelope keys: an ALB-wrapped attacker request ALWAYS has
    # "headers"/"requestContext", and its JSON body lands in event["body"] as
    # a string — so top-level keys are unspoofable through the ALB, and a body
    # containing gpbot_async falls through to normal signature verification.
    if event.get("gpbot_async") and "headers" not in event and "requestContext" not in event:
        return handle_async_processing(event)

    # LOG POISONING GUARD: the endpoint is public and the CloudWatch metric
    # filter (infrastructure/modules/clickup-bot/main.tf) matches "ERROR" /
    # "Failed to" anywhere in ANY log line in this log group. Never echo
    # request content (body, headers, event type, history_items) before
    # signature verification — an attacker could otherwise fire the fail-loud
    # alarm, or drown it in false positives, straight from the request body.
    print("Received webhook event")

    headers = event.get("headers", {})
    signature = get_header_case_insensitive(headers, "x-signature")
    raw_body = event.get("body", "{}")

    body = raw_body
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except json.JSONDecodeError:
            print("Invalid JSON in webhook body")
            return {"statusCode": 400, "body": json.dumps({"error": "invalid JSON body"})}

    # Valid JSON that is not an object ("[]", "null", "42", '"x"') would crash
    # body.get() below with an AttributeError BEFORE signature verification.
    # The Lambda runtime logs unhandled exceptions as "[ERROR] ...", which the
    # alarm metric filter matches — so an unauthenticated client could fire or
    # drown the fail-loud alarm at will. Same quiet wording as the branch above.
    if not isinstance(body, dict):
        print("Invalid JSON in webhook body")
        return {"statusCode": 400, "body": json.dumps({"error": "invalid JSON body"})}

    # Filter irrelevant deliveries BEFORE signature verification, which needs
    # Secrets Manager: during a secrets outage only gpbot-tagged deliveries may
    # fail. If every workspace delivery 500s, ClickUp's consecutive-failure
    # tracking suspends the webhook and the bot stays dead even after the
    # outage is fixed (see README, "After an outage"). Skipping these
    # unverified is safe: the skip branches perform no action.
    event_type = body.get("event")
    if event_type != "taskTagUpdated":
        print("Skipping delivery: not a taskTagUpdated event")
        return {"statusCode": 200, "body": json.dumps({"skipped": "not a tag update"})}

    matched_tag = find_matched_tag(body.get("history_items", []))
    if not matched_tag:
        print("Skipping delivery: no target tag in history_items")
        return {"statusCode": 200, "body": json.dumps({"skipped": "not a target tag"})}

    # Direct invocations (console/tests) can pass body as an already-parsed
    # dict; verifying a dict would raise AttributeError inside
    # verify_webhook_signature and be misclassified below as a secrets outage.
    # Re-serializing can never match the HMAC, so this ends in a clean 401.
    if not isinstance(raw_body, str):
        raw_body = json.dumps(raw_body)

    try:
        signature_valid = verify_webhook_signature(raw_body, signature)
    except Exception as e:
        # Secrets Manager failure (throttle, stripped IAM) must be loud and
        # distinguishable from a signature mismatch, and must NOT be cached:
        # the next invocation retries the fetch.
        print(f"ERROR: Secrets unavailable, cannot verify webhook signature: {e}")
        return {"statusCode": 500, "body": json.dumps({"error": "secrets unavailable"})}

    if not signature_valid:
        # ERROR prefix feeds the CloudWatch alarm (metric filter in
        # infrastructure/modules/clickup-bot/main.tf): sustained 401s mean a
        # rotated/mismatched CLICKUP_WEBHOOK_SECRET and end in ClickUp
        # suspending the webhook — silently, unless this line alarms.
        print("ERROR: Webhook signature verification failed")
        return {"statusCode": 401, "body": json.dumps({"error": "Unauthorized"})}

    # The request is now authenticated, so echoing its content can no longer let
    # an attacker poison the alarm (see the log-poisoning guard above). Log the
    # event for debugging, but redact the signature — see redact_signature.
    print(f"Verified webhook event: {json.dumps(redact_signature(event), default=str)}")

    task_id = body.get("task_id")
    if not task_id:
        print("Missing task_id in webhook payload")
        return {"statusCode": 400, "body": json.dumps({"error": "missing task_id"})}

    # FAST-ACK: the request is authenticated and the work item is validated
    # (task_id present, matched_tag in TAG_CONFIG) — answer ClickUp NOW, with
    # zero ClickUp API calls in-path, and let the async worker do the rest.
    # See enqueue_async_processing for the incident rationale.
    if enqueue_async_processing(task_id, matched_tag):
        return {
            "statusCode": 200,
            "body": json.dumps({"status": "accepted", "task_id": task_id, "label": TAG_CONFIG[matched_tag]["label"]}),
        }

    # Synchronous fallback (initial prod state until the self-invoke IAM
    # lands): same shared dedup-then-trigger path the async worker uses.
    return dedup_check_then_trigger(task_id, matched_tag)


def trigger_fargate_task(
    task_id: str, instruction: str, label: str, model: str = "sonnet", retry_ack: bool = False
) -> dict:
    # retry_ack: True ONLY from the async worker, where ClickUp already has
    # its 200. The synchronous fallback (the guaranteed initial prod state
    # until the self-invoke IAM lands) runs while ClickUp is still waiting on
    # the webhook response — the exact path whose slowness caused the
    # 2026-07-14 retry storm — so it must never sleep or double-post.
    ecs_client = boto3.client("ecs")

    cluster_arn = os.environ.get("ECS_CLUSTER_ARN")
    task_definition = os.environ.get("ECS_TASK_DEFINITION")
    subnet_ids = [s for s in os.environ.get("ECS_SUBNET_IDS", "").split(",") if s]
    security_group_id = os.environ.get("ECS_SECURITY_GROUP_ID")

    if not all([cluster_arn, task_definition, subnet_ids, security_group_id]):
        error_msg = "ECS configuration is missing or incomplete; the bot is misconfigured and cannot start"
        print(f"ERROR: {error_msg}")
        post_failure_comment(task_id, error_msg)
        return {"statusCode": 500, "body": json.dumps({"error": error_msg})}

    print(f"Triggering Fargate task for {task_id} with model={model}, label={label}")

    try:
        response = ecs_client.run_task(
            cluster=cluster_arn,
            taskDefinition=task_definition,
            launchType="FARGATE",
            tags=[{"key": "Project", "value": "clickup-bot"}],
            networkConfiguration={
                "awsvpcConfiguration": {
                    "subnets": subnet_ids,
                    "securityGroups": [security_group_id],
                    "assignPublicIp": "DISABLED",
                }
            },
            overrides={
                "containerOverrides": [
                    {
                        "name": "engineer-agent",
                        "environment": [
                            {"name": "CLICKUP_TASK_ID", "value": task_id},
                            {"name": "INSTRUCTION", "value": instruction},
                            {"name": "AGENT_MODEL", "value": model},
                        ],
                    }
                ]
            },
        )

        failures = response.get("failures", [])
        tasks = response.get("tasks", [])

        if failures:
            # Raw failures[].reason strings can embed ARNs/account details:
            # log them, but keep the comment and response generic (same
            # treatment as the except path below).
            failure_reasons = [f.get("reason", "unknown") for f in failures]
            print(f"ERROR: ECS task launch failed: {', '.join(failure_reasons)}")
            error_msg = f"ECS task launch failed ({len(failures)} failure(s)); details in CloudWatch logs"
            post_failure_comment(task_id, error_msg)
            return {"statusCode": 500, "body": json.dumps({"error": error_msg})}

        if not tasks:
            error_msg = "ECS run_task returned no tasks and no failures"
            print(f"ERROR: {error_msg}")
            post_failure_comment(task_id, error_msg)
            return {"statusCode": 500, "body": json.dumps({"error": error_msg})}

        task_arn = tasks[0]["taskArn"]
        print(f"Started Fargate task: {task_arn}")

        # Posted only AFTER a successful launch: this comment is the dedup
        # marker, so it must never exist on a task whose launch failed. A
        # failed ack post must not fail the invocation (the task is running).
        # RETRY ONCE, ASYNC WORKER ONLY (2026-07-14): a missing marker leaves
        # the retry-storm window open, and in the async worker ClickUp already
        # has its 200, so a brief pause + one retry is free there. In the
        # synchronous fallback ClickUp is STILL WAITING — a sleep + second 10s
        # POST would add up to 12s to a path that already exceeded ClickUp's
        # webhook timeout (the incident's root cause) — so it posts exactly
        # once, like the pre-fast-ack handler did. Only the FINAL failure logs
        # the alarm-matching line — swallowed ack failures are exactly what
        # the alarm exists for (it alarmed correctly during the incident).
        # The async first-attempt line is deliberately quiet: only the
        # exception TYPE is logged (a message could contain alarm terms).
        ack_text = f"{PROCESSING_STARTED_PREFIX} ({label}, model: {model})..."
        try:
            post_comment(task_id, ack_text)
        except Exception as first_err:
            if retry_ack:
                print(f"Starting-comment post hit {type(first_err).__name__}, retrying once")
                time.sleep(ACK_COMMENT_RETRY_DELAY_SECONDS)
                try:
                    post_comment(task_id, ack_text)
                except Exception as e:
                    print(f"Failed to post starting comment: {e}")
            else:
                print(f"Failed to post starting comment: {first_err}")

        return {
            "statusCode": 200,
            "body": json.dumps(
                {"status": "triggered", "task_id": task_id, "label": label, "fargate_task_arn": task_arn}
            ),
        }

    except Exception as e:
        # Raw boto3 exception text carries ARNs, request IDs, and sometimes
        # credential context. Keep the full detail in the logs (CloudWatch), but
        # put only the exception type into the public ClickUp comment and the
        # HTTP response — a generic pointer, not the leak-prone message.
        print(f"Failed to start Fargate task: {e}")
        post_failure_comment(task_id, f"{type(e).__name__} (see CloudWatch logs for details)")
        return {"statusCode": 500, "body": json.dumps({"error": f"failed to start task: {type(e).__name__}"})}
