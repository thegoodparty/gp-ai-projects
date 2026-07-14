"""Behavioral tests for the clickup_bot webhook Lambda handler.

Written from the behavioral contract only (3-agent pattern: test writer never
reads the source). Each test encodes one numbered behavior from the spec.
"""

import hashlib
import hmac
import json
import time
from urllib.error import HTTPError, URLError

import handler
import pytest

TEST_SECRET = "test-secret"
TEST_API_KEY = "test-key"
TASK_ARN = "arn:aws:ecs:us-east-1:123456789012:task/gpbot/abc123def456"

ECS_ENV = {
    "ECS_CLUSTER_ARN": "arn:aws:ecs:us-east-1:123456789012:cluster/gpbot-cluster",
    "ECS_TASK_DEFINITION": "gpbot-engineer-agent:7",
    "ECS_SUBNET_IDS": "subnet-aaa111,subnet-bbb222",
    "ECS_SECURITY_GROUP_ID": "sg-0123456789",
}


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeHTTPResponse:
    """Context-manager response whose read() returns JSON bytes."""

    def __init__(self, payload: dict):
        self._payload = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class FakeUrlopen:
    """Fake for handler.urlopen. Records every ClickUp API call.

    calls: list of (method, url, decoded_body_dict_or_None)
    """

    def __init__(self):
        self.calls = []
        self.requests = []  # raw Request objects, for header assertions
        self.comments_response = {"comments": []}
        self.get_comments_error = None  # exception to raise on GET .../comment
        self.post_comment_error = None  # exception to raise on POST .../comment

    def __call__(self, request, timeout=None, **kwargs):
        method = request.get_method()
        url = request.full_url
        body = json.loads(request.data.decode()) if request.data else None
        self.calls.append((method, url, body))
        self.requests.append(request)

        if method == "GET" and "/comment" in url:
            if self.get_comments_error is not None:
                raise self.get_comments_error
            return FakeHTTPResponse(self.comments_response)
        if method == "POST" and "/comment" in url:
            if self.post_comment_error is not None:
                raise self.post_comment_error
            return FakeHTTPResponse({"id": "new-comment-1"})
        return FakeHTTPResponse({})

    @property
    def posted_comments(self) -> list[dict]:
        """Bodies of every POST .../comment call."""
        return [body for method, url, body in self.calls if method == "POST" and "/comment" in url]

    @property
    def posted_comment_texts(self) -> list[str]:
        return [body["comment_text"] for body in self.posted_comments]

    @property
    def authorization_headers(self) -> list[str | None]:
        """Authorization header value for every recorded ClickUp API request."""
        return [
            next((value for key, value in request.headers.items() if key.lower() == "authorization"), None)
            for request in self.requests
        ]


class FakeECSClient:
    def __init__(self):
        self.run_task_calls = []
        self.response = {"tasks": [{"taskArn": TASK_ARN}], "failures": []}
        self.exception = None

    def run_task(self, **kwargs):
        self.run_task_calls.append(kwargs)
        if self.exception is not None:
            raise self.exception
        return self.response


class FakeBoto3ClientFactory:
    """Stands in for boto3.client; hands back the fake ECS client."""

    def __init__(self, ecs_client: FakeECSClient):
        self._ecs_client = ecs_client
        self.requested_services = []

    def __call__(self, service_name, *args, **kwargs):
        self.requested_services.append(service_name)
        return self._ecs_client


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def secrets_cache():
    handler._secrets_cache = {
        "CLICKUP_API_KEY": TEST_API_KEY,
        "CLICKUP_WEBHOOK_SECRET": TEST_SECRET,
    }
    yield
    handler._secrets_cache = None


@pytest.fixture(autouse=True)
def clean_ecs_env(monkeypatch):
    for var in ECS_ENV:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def fake_clickup(monkeypatch):
    fake = FakeUrlopen()
    monkeypatch.setattr(handler, "urlopen", fake)
    return fake


@pytest.fixture(autouse=True)
def fake_ecs(monkeypatch):
    ecs = FakeECSClient()
    monkeypatch.setattr(handler.boto3, "client", FakeBoto3ClientFactory(ecs))
    return ecs


@pytest.fixture
def ecs_env(clean_ecs_env, monkeypatch):
    for key, value in ECS_ENV.items():
        monkeypatch.setenv(key, value)
    return ECS_ENV


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def sign(body: str, secret: str = TEST_SECRET) -> str:
    return hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


def make_event(body_dict: dict, signature: str | None = None, header_name: str = "x-signature") -> dict:
    body = json.dumps(body_dict)
    if signature is None:
        signature = sign(body)
    return {"headers": {header_name: signature}, "body": body}


def tag_updated_body(task_id: str | None = "abc123", tags: tuple = ("gpbot-analyze",), history_items=None) -> dict:
    if history_items is None:
        history_items = [{"field": "tag", "after": [{"name": tag} for tag in tags]}]
    body = {"event": "taskTagUpdated", "history_items": history_items}
    if task_id is not None:
        body["task_id"] = task_id
    return body


def response_body(resp: dict) -> dict:
    return json.loads(resp["body"])


def engineer_agent_env(run_task_kwargs: dict) -> dict:
    """Extract {name: value} env of the engineer-agent container override."""
    container_overrides = run_task_kwargs["overrides"]["containerOverrides"]
    matches = [c for c in container_overrides if c.get("name") == "engineer-agent"]
    assert len(matches) == 1, f"expected exactly one engineer-agent container override, got: {container_overrides}"
    return {entry["name"]: entry["value"] for entry in matches[0]["environment"]}


def existing_gpbot_comment_response(
    age_seconds: float = 30.0,
    include_comment_text: bool = True,
    with_type_key: bool = False,
) -> dict:
    """REAL ClickUp GET /task/{id}/comment shape, captured live during the
    2026-07-14 duplicate-launch incident (task DATA-2108): comment[] items have
    NO "type" key, and "date" is a STRING of epoch milliseconds. The previous
    fixture invented a "type": "text" field the real API never sends, so the
    dedup matcher passed its test while matching 0 real comments in prod
    (oracle problem). Never add fields here that a live capture doesn't show.

    with_type_key adds the (fabricated) "type" key back to prove forward-compat
    if ClickUp ever ships one; include_comment_text=False exercises the
    item-concatenation fallback path.
    """
    text = "[GP-Bot] Processing started (analyze, model: opus)..."
    item: dict = {"text": text}
    if with_type_key:
        item["type"] = "text"
    comment: dict = {
        "id": "90130291038679",
        "comment": [item],
        "user": {"id": 105985359, "username": "Collin Park"},
        "date": str(int((time.time() - age_seconds) * 1000)),
        "reply_count": 0,
    }
    if include_comment_text:
        comment["comment_text"] = text
    return {"comments": [comment]}


def assert_no_side_effects(fake_clickup: FakeUrlopen, fake_ecs: FakeECSClient):
    assert fake_ecs.run_task_calls == []
    assert fake_clickup.posted_comments == []


# The CloudWatch alarm leg of fail-loud: infrastructure/modules/clickup-bot/main.tf
# creates a log metric filter with pattern ?"ERROR" ?"Failed to" and alarms on any
# match in this Lambda's log group. These helpers are the test-side half of that
# contract:
#   - every failure path must emit a line containing one of those terms, or the
#     alarm never fires and the failure is operationally silent;
#   - unauthenticated request content must never be echoed to the logs, or any
#     internet client could fire (or drown) the alarm by sending "ERROR" in a body.
# If a handler log line is reworded, update the terraform pattern and these terms
# together.
ALARM_FILTER_TERMS = ("ERROR", "Failed to")

# Marker an attacker would embed in a request to poison the alarm log filter.
ALARM_POISON = "ERROR Failed to poison"


def assert_alarm_log_emitted(capsys) -> str:
    out = capsys.readouterr().out
    assert any(term in out for term in ALARM_FILTER_TERMS), f"no alarm-matching log line in: {out!r}"
    return out


def assert_no_alarm_log_emitted(capsys) -> str:
    out = capsys.readouterr().out
    assert not any(term in out for term in ALARM_FILTER_TERMS), f"alarm-matching log line leaked: {out!r}"
    return out


# ---------------------------------------------------------------------------
# 1. Signature verification
# ---------------------------------------------------------------------------


def test_invalid_signature_returns_401(fake_clickup, fake_ecs, ecs_env, capsys):
    event = make_event(tag_updated_body(), signature="0" * 64)

    resp = handler.handler(event, None)

    assert resp["statusCode"] == 401
    assert fake_clickup.calls == []
    assert fake_ecs.run_task_calls == []
    # A rotated/mismatched CLICKUP_WEBHOOK_SECRET 401s every gpbot delivery until
    # ClickUp suspends the webhook — this class MUST fire the CloudWatch alarm.
    assert_alarm_log_emitted(capsys)


def test_missing_signature_header_returns_401(fake_clickup, fake_ecs, ecs_env, capsys):
    body = json.dumps(tag_updated_body())
    event = {"headers": {}, "body": body}

    resp = handler.handler(event, None)

    assert resp["statusCode"] == 401
    assert fake_clickup.calls == []
    assert fake_ecs.run_task_calls == []
    assert_alarm_log_emitted(capsys)


def test_non_ascii_signature_returns_401_not_500(fake_clickup, fake_ecs, ecs_env, capsys):
    # hmac.compare_digest raises TypeError on non-ASCII str input. The x-signature
    # header is attacker-controlled, so a malformed signature is an invalid
    # signature (401) — never a secrets outage (500), which would page ops with
    # the wrong runbook while Secrets Manager is healthy.
    event = make_event(tag_updated_body(), signature="café")

    resp = handler.handler(event, None)

    assert resp["statusCode"] == 401
    assert response_body(resp)["error"] == "Unauthorized"
    assert fake_clickup.calls == []
    assert fake_ecs.run_task_calls == []
    out = assert_alarm_log_emitted(capsys)
    assert "secrets unavailable" not in out.lower()


def test_signature_header_lookup_is_case_insensitive(fake_clickup, fake_ecs, ecs_env):
    event = make_event(tag_updated_body(), header_name="X-Signature")

    resp = handler.handler(event, None)

    assert resp["statusCode"] == 200
    assert len(fake_ecs.run_task_calls) == 1


# ---------------------------------------------------------------------------
# 2. Missing webhook secret
# ---------------------------------------------------------------------------


def test_missing_webhook_secret_returns_401(fake_clickup, fake_ecs, ecs_env, capsys):
    handler._secrets_cache = {"CLICKUP_API_KEY": "test-key"}
    event = make_event(tag_updated_body())

    resp = handler.handler(event, None)

    assert resp["statusCode"] == 401
    assert fake_clickup.calls == []
    assert fake_ecs.run_task_calls == []
    assert_alarm_log_emitted(capsys)


# ---------------------------------------------------------------------------
# 3. Non-taskTagUpdated events are skipped
# ---------------------------------------------------------------------------


def test_other_event_type_returns_200_without_side_effects(fake_clickup, fake_ecs, ecs_env):
    body = {"event": "taskCreated", "task_id": "abc123", "history_items": []}
    event = make_event(body)

    resp = handler.handler(event, None)

    assert resp["statusCode"] == 200
    assert_no_side_effects(fake_clickup, fake_ecs)


# ---------------------------------------------------------------------------
# 4. Missing task_id
# ---------------------------------------------------------------------------


def test_missing_task_id_returns_400(fake_clickup, fake_ecs, ecs_env):
    event = make_event(tag_updated_body(task_id=None))

    resp = handler.handler(event, None)

    assert resp["statusCode"] == 400
    assert fake_ecs.run_task_calls == []
    assert fake_clickup.posted_comments == []


# ---------------------------------------------------------------------------
# 5. Non-gpbot tags are skipped
# ---------------------------------------------------------------------------


def test_non_gpbot_tag_returns_200_without_side_effects(fake_clickup, fake_ecs, ecs_env):
    event = make_event(tag_updated_body(tags=("needs-grooming",)))

    resp = handler.handler(event, None)

    assert resp["statusCode"] == 200
    assert_no_side_effects(fake_clickup, fake_ecs)


# ---------------------------------------------------------------------------
# 6. tag_removed entries never trigger
# ---------------------------------------------------------------------------


def test_tag_removed_entry_never_triggers(fake_clickup, fake_ecs, ecs_env):
    history_items = [{"field": "tag_removed", "after": [{"name": "gpbot-analyze"}]}]
    event = make_event(tag_updated_body(history_items=history_items))

    resp = handler.handler(event, None)

    assert resp["statusCode"] == 200
    assert_no_side_effects(fake_clickup, fake_ecs)


# ---------------------------------------------------------------------------
# 7. gpbot-analyze launches the Fargate engineer agent
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tag_name", ["gpbot-analyze", "GPBot-Analyze"])
def test_gpbot_analyze_launches_fargate_task(fake_clickup, fake_ecs, ecs_env, tag_name):
    event = make_event(tag_updated_body(tags=(tag_name,)))

    resp = handler.handler(event, None)

    assert resp["statusCode"] == 200
    assert response_body(resp)["fargate_task_arn"] == TASK_ARN

    # Exactly one processing-started comment on the ClickUp task.
    assert len(fake_clickup.posted_comments) == 1
    comment = fake_clickup.posted_comments[0]
    assert comment["comment_text"].startswith("[GP-Bot] Processing started")
    assert "analyze" in comment["comment_text"]
    assert "opus" in comment["comment_text"]
    assert comment["notify_all"] is False

    # Exactly one Fargate launch with the configured infrastructure.
    assert len(fake_ecs.run_task_calls) == 1
    kwargs = fake_ecs.run_task_calls[0]
    assert kwargs["cluster"] == ECS_ENV["ECS_CLUSTER_ARN"]
    assert kwargs["taskDefinition"] == ECS_ENV["ECS_TASK_DEFINITION"]
    assert kwargs["launchType"] == "FARGATE"

    container_env = engineer_agent_env(kwargs)
    assert container_env["CLICKUP_TASK_ID"] == "abc123"
    assert container_env["AGENT_MODEL"] == "opus"
    # The INSTRUCTION content IS the behavioral difference between the two
    # tags: gpbot-analyze must ship the analyze contract, not the implement one.
    assert "Analyze and Report" in container_env["INSTRUCTION"]
    assert "Implement and Create PR" not in container_env["INSTRUCTION"]
    # Repo guidance (omni monorepo / archived repos) is deliberately NOT in the
    # INSTRUCTION: it is single-sourced in the agent's capability prompt and
    # pinned by engineer_agent/tests/test_config.py.

    vpc_config = kwargs["networkConfiguration"]["awsvpcConfiguration"]
    assert vpc_config["subnets"] == ["subnet-aaa111", "subnet-bbb222"]
    assert vpc_config["securityGroups"] == ["sg-0123456789"]

    assert {"key": "Project", "value": "clickup-bot"} in kwargs["tags"]


# ---------------------------------------------------------------------------
# 8. gpbot-work launches with an "implement" comment
# ---------------------------------------------------------------------------


def test_gpbot_work_launches_with_implement_comment(fake_clickup, fake_ecs, ecs_env):
    event = make_event(tag_updated_body(tags=("gpbot-work",)))

    resp = handler.handler(event, None)

    assert resp["statusCode"] == 200
    assert response_body(resp)["fargate_task_arn"] == TASK_ARN

    assert len(fake_clickup.posted_comments) == 1
    comment = fake_clickup.posted_comments[0]
    assert comment["comment_text"].startswith("[GP-Bot] Processing started")
    assert "implement" in comment["comment_text"]

    assert len(fake_ecs.run_task_calls) == 1
    kwargs = fake_ecs.run_task_calls[0]
    assert kwargs["cluster"] == ECS_ENV["ECS_CLUSTER_ARN"]
    assert kwargs["taskDefinition"] == ECS_ENV["ECS_TASK_DEFINITION"]
    assert kwargs["launchType"] == "FARGATE"

    container_env = engineer_agent_env(kwargs)
    assert container_env["CLICKUP_TASK_ID"] == "abc123"
    assert container_env["AGENT_MODEL"] == "opus"
    # gpbot-work must ship the implement contract (PR + branch naming), and
    # must NOT receive the analyze instruction.
    assert "Implement and Create PR" in container_env["INSTRUCTION"]
    assert "gp-bot_" in container_env["INSTRUCTION"]
    assert "Analyze and Report" not in container_env["INSTRUCTION"]
    # Repo guidance (omni/archived) lives in the agent's capability prompt
    # (engineer_agent/agent/config.py — pinned by engineer_agent/tests), NOT
    # here: single source. The implement contract must drive every change
    # with a failing test first (red/green TDD)...
    assert "failing test" in container_env["INSTRUCTION"]
    # ...and self-review the finished diff against the repo's ai-rules files
    # before opening the PR.
    assert "ai-rules" in container_env["INSTRUCTION"]


# ---------------------------------------------------------------------------
# 9. Existing [GP-Bot] comment means already processed -> skip
# ---------------------------------------------------------------------------


def test_existing_gpbot_comment_skips_processing(fake_clickup, fake_ecs, ecs_env):
    # Fixture is the REAL API shape (no "type" key on comment items). During the
    # 2026-07-14 incident the matcher required item["type"] == "text" and so
    # matched 0 of 13 real comments — including 6 of the bot's own ack comments —
    # letting one retried webhook delivery launch 6 Fargate agents.
    fake_clickup.comments_response = existing_gpbot_comment_response()
    event = make_event(tag_updated_body())

    resp = handler.handler(event, None)

    assert resp["statusCode"] == 200
    assert fake_ecs.run_task_calls == []
    assert fake_clickup.posted_comments == []


def test_dedup_matches_via_item_concatenation_when_comment_text_absent(fake_clickup, fake_ecs, ecs_env):
    # Defensive fallback: if ClickUp ever omits the top-level comment_text, the
    # matcher must derive the text by concatenating item["text"] WITHOUT
    # filtering on a "type" key the real API does not send.
    fake_clickup.comments_response = existing_gpbot_comment_response(include_comment_text=False)
    event = make_event(tag_updated_body())

    resp = handler.handler(event, None)

    assert resp["statusCode"] == 200
    assert fake_ecs.run_task_calls == []
    assert fake_clickup.posted_comments == []


def test_dedup_tolerates_type_keyed_items_forward_compat(fake_clickup, fake_ecs, ecs_env):
    # Forward-compat: if ClickUp ever ADDS a "type" key to comment items, its
    # presence must not break the matcher either.
    fake_clickup.comments_response = existing_gpbot_comment_response(include_comment_text=False, with_type_key=True)
    event = make_event(tag_updated_body())

    resp = handler.handler(event, None)

    assert resp["statusCode"] == 200
    assert fake_ecs.run_task_calls == []
    assert fake_clickup.posted_comments == []


# ---------------------------------------------------------------------------
# 10. Comment fetch failure -> 500
# ---------------------------------------------------------------------------


def test_comment_fetch_http_error_returns_500(fake_clickup, fake_ecs, ecs_env, capsys):
    fake_clickup.get_comments_error = HTTPError("http://x", 500, "err", {}, None)
    event = make_event(tag_updated_body())

    resp = handler.handler(event, None)

    assert resp["statusCode"] == 500
    assert fake_ecs.run_task_calls == []
    assert_alarm_log_emitted(capsys)


# ---------------------------------------------------------------------------
# 11-13. run_task failure modes -> failure comment + 500
# ---------------------------------------------------------------------------


def assert_failed_loudly(resp: dict, fake_clickup: FakeUrlopen, capsys):
    assert resp["statusCode"] == 500
    failure_comments = [
        text for text in fake_clickup.posted_comment_texts if text.startswith("[GP-Bot] Failed to start processing")
    ]
    assert len(failure_comments) == 1
    assert_alarm_log_emitted(capsys)


def test_run_task_failures_posts_failure_comment_and_returns_500(fake_clickup, fake_ecs, ecs_env, capsys):
    fake_ecs.response = {"tasks": [], "failures": [{"arn": "arn:x", "reason": "RESOURCE:MEMORY"}]}
    event = make_event(tag_updated_body())

    resp = handler.handler(event, None)

    assert_failed_loudly(resp, fake_clickup, capsys)


def test_run_task_empty_response_posts_failure_comment_and_returns_500(fake_clickup, fake_ecs, ecs_env, capsys):
    fake_ecs.response = {"tasks": [], "failures": []}
    event = make_event(tag_updated_body())

    resp = handler.handler(event, None)

    assert_failed_loudly(resp, fake_clickup, capsys)


def test_run_task_exception_posts_failure_comment_and_returns_500(fake_clickup, fake_ecs, ecs_env, capsys):
    fake_ecs.exception = RuntimeError("ECS exploded")
    event = make_event(tag_updated_body())

    resp = handler.handler(event, None)

    assert_failed_loudly(resp, fake_clickup, capsys)


# ---------------------------------------------------------------------------
# 14. FAIL-LOUD: missing/incomplete ECS config must comment failure + 500
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("missing_var", list(ECS_ENV))
@pytest.mark.parametrize("mode", ["unset", "empty"])
def test_missing_ecs_config_fails_loud(fake_clickup, fake_ecs, ecs_env, monkeypatch, missing_var, mode, capsys):
    if mode == "unset":
        monkeypatch.delenv(missing_var)
    else:
        monkeypatch.setenv(missing_var, "")
    event = make_event(tag_updated_body())

    resp = handler.handler(event, None)

    assert resp["statusCode"] == 500
    failure_comments = [
        text for text in fake_clickup.posted_comment_texts if text.startswith("[GP-Bot] Failed to start processing")
    ]
    assert len(failure_comments) == 1
    assert fake_ecs.run_task_calls == []
    assert_alarm_log_emitted(capsys)


def test_all_ecs_config_missing_fails_loud(fake_clickup, fake_ecs, capsys):
    # No ecs_env fixture: none of the four ECS_* vars are set at all.
    event = make_event(tag_updated_body())

    resp = handler.handler(event, None)

    assert resp["statusCode"] == 500
    failure_comments = [
        text for text in fake_clickup.posted_comment_texts if text.startswith("[GP-Bot] Failed to start processing")
    ]
    assert len(failure_comments) == 1
    assert fake_ecs.run_task_calls == []
    assert_alarm_log_emitted(capsys)


# ---------------------------------------------------------------------------
# 15. tag_removed entry before a valid tag entry still triggers
# ---------------------------------------------------------------------------


def test_tag_removed_before_tag_added_still_triggers(fake_clickup, fake_ecs, ecs_env):
    history_items = [
        {"field": "tag_removed", "after": [{"name": "gpbot-analyze"}]},
        {"field": "tag", "after": [{"name": "gpbot-analyze"}]},
    ]
    event = make_event(tag_updated_body(history_items=history_items))

    resp = handler.handler(event, None)

    assert resp["statusCode"] == 200
    assert response_body(resp)["fargate_task_arn"] == TASK_ARN
    assert len(fake_ecs.run_task_calls) == 1
    assert len(fake_clickup.posted_comments) == 1
    assert fake_clickup.posted_comments[0]["comment_text"].startswith("[GP-Bot] Processing started")


# ---------------------------------------------------------------------------
# 16. Failure comments must NOT block retries (fail-loud must stay retryable)
# ---------------------------------------------------------------------------


def failure_comment_response(reason: str = "ECS configuration is missing or incomplete") -> dict:
    # Real API shape: no "type" key on items, comment_text present, string epoch-ms date.
    text = f"[GP-Bot] Failed to start processing: {reason}"
    return {
        "comments": [
            {
                "id": "90130291038680",
                "comment": [{"text": text}],
                "comment_text": text,
                "user": {"id": 105985359, "username": "Collin Park"},
                "date": str(int((time.time() - 30) * 1000)),
                "reply_count": 0,
            }
        ]
    }


def test_prior_failure_comment_does_not_block_retry(fake_clickup, fake_ecs, ecs_env):
    fake_clickup.comments_response = failure_comment_response()
    event = make_event(tag_updated_body())

    resp = handler.handler(event, None)

    assert resp["statusCode"] == 200
    assert response_body(resp)["fargate_task_arn"] == TASK_ARN
    assert len(fake_ecs.run_task_calls) == 1
    assert any(text.startswith("[GP-Bot] Processing started") for text in fake_clickup.posted_comment_texts)


def test_retry_after_config_fixed_processes_task(fake_clickup, fake_ecs, monkeypatch):
    # First attempt: no ECS config at all -> fail loud with a failure comment.
    event = make_event(tag_updated_body())
    resp = handler.handler(event, None)

    assert resp["statusCode"] == 500
    failure_texts = [
        text for text in fake_clickup.posted_comment_texts if text.startswith("[GP-Bot] Failed to start processing")
    ]
    assert len(failure_texts) == 1
    assert fake_ecs.run_task_calls == []

    # Ops fixes the config; the failure comment is now on the task; the user
    # re-adds the tag. The retry must process, not skip as 'already processed'.
    for key, value in ECS_ENV.items():
        monkeypatch.setenv(key, value)
    fake_clickup.comments_response = {
        "comments": [
            {
                "id": "90130291038681",
                "comment": [{"text": failure_texts[0]}],
                "comment_text": failure_texts[0],
                "user": {"id": 105985359, "username": "Collin Park"},
                "date": str(int((time.time() - 30) * 1000)),
                "reply_count": 0,
            }
        ]
    }

    resp = handler.handler(event, None)

    assert resp["statusCode"] == 200
    assert response_body(resp)["fargate_task_arn"] == TASK_ARN
    assert len(fake_ecs.run_task_calls) == 1


def test_run_task_failure_leaves_no_processing_started_marker(fake_clickup, fake_ecs, ecs_env):
    # If the launch fails, no 'Processing started' comment may exist on the
    # task, otherwise the dedup check would block the retry.
    fake_ecs.exception = RuntimeError("ECS exploded")
    event = make_event(tag_updated_body())

    resp = handler.handler(event, None)

    assert resp["statusCode"] == 500
    assert not any(text.startswith("[GP-Bot] Processing started") for text in fake_clickup.posted_comment_texts)


# ---------------------------------------------------------------------------
# 17. Non-HTTPError network failures must not crash the handler
# ---------------------------------------------------------------------------


def test_comment_fetch_urlerror_returns_500(fake_clickup, fake_ecs, ecs_env, capsys):
    fake_clickup.get_comments_error = URLError("connection refused")
    event = make_event(tag_updated_body())

    resp = handler.handler(event, None)

    assert resp["statusCode"] == 500
    assert response_body(resp)["error"] == "failed to get comments"
    assert fake_ecs.run_task_calls == []
    assert_alarm_log_emitted(capsys)


def test_ack_comment_failure_does_not_prevent_launch(fake_clickup, fake_ecs, ecs_env, capsys):
    fake_clickup.post_comment_error = TimeoutError("read timed out")
    event = make_event(tag_updated_body())

    resp = handler.handler(event, None)

    assert resp["statusCode"] == 200
    assert response_body(resp)["fargate_task_arn"] == TASK_ARN
    assert len(fake_ecs.run_task_calls) == 1
    # The swallowed ack-post failure is invisible to the caller (200), so the
    # alarm log line is its ONLY operational signal.
    assert_alarm_log_emitted(capsys)


def test_missing_config_failure_comment_urlerror_still_returns_500(fake_clickup, fake_ecs, capsys):
    # No ecs_env fixture: config missing. Posting the failure comment blows up
    # with a connection error; the handler must still return the structured 500.
    fake_clickup.post_comment_error = URLError("connection refused")
    event = make_event(tag_updated_body())

    resp = handler.handler(event, None)

    assert resp["statusCode"] == 500
    assert "error" in response_body(resp)
    assert fake_ecs.run_task_calls == []
    # When the failure comment itself cannot be posted, the user gets nothing:
    # BOTH the config error and the swallowed comment failure must reach the
    # alarm-matching logs.
    out = assert_alarm_log_emitted(capsys)
    assert "Failed to post failure comment" in out


# ---------------------------------------------------------------------------
# 18. Secrets fetch failure: distinct 500, and the failure is never cached
# ---------------------------------------------------------------------------


class FakeSecretsManagerClient:
    def __init__(self):
        self.exception = None
        self.calls = 0
        self.secret_string = json.dumps({"CLICKUP_API_KEY": "test-key", "CLICKUP_WEBHOOK_SECRET": TEST_SECRET})

    def get_secret_value(self, SecretId):
        self.calls += 1
        if self.exception is not None:
            raise self.exception
        return {"SecretString": self.secret_string}


def test_secrets_fetch_failure_returns_500_and_is_not_cached(fake_clickup, fake_ecs, ecs_env, monkeypatch, capsys):
    handler._secrets_cache = None
    secrets = FakeSecretsManagerClient()
    secrets.exception = RuntimeError("throttled")
    ecs = FakeECSClient()

    def client_factory(service_name, *args, **kwargs):
        if service_name == "secretsmanager":
            return secrets
        return ecs

    monkeypatch.setattr(handler.boto3, "client", client_factory)
    event = make_event(tag_updated_body())

    # Secrets unavailable must be a 500, distinguishable from a 401
    # signature mismatch.
    resp = handler.handler(event, None)
    assert resp["statusCode"] == 500
    assert response_body(resp)["error"] == "secrets unavailable"
    assert ecs.run_task_calls == []
    # During a secrets outage no failure comment can be posted; the alarm log
    # line is the only feedback anyone gets.
    assert_alarm_log_emitted(capsys)

    # Secrets Manager recovers: the same warm container must retry the fetch
    # (the failure must not have been cached as {}) and process normally.
    secrets.exception = None
    resp = handler.handler(event, None)
    assert resp["statusCode"] == 200
    assert response_body(resp)["fargate_task_arn"] == TASK_ARN
    assert len(ecs.run_task_calls) == 1


# ---------------------------------------------------------------------------
# 19. Secrets outage must not fail deliveries the bot would ignore anyway.
# ClickUp suspends a webhook after sustained consecutive delivery failures;
# if EVERY workspace delivery 500s during a secrets outage, the webhook gets
# suspended and the bot stays dead after the outage is fixed. Only deliveries
# that would actually trigger the bot may fail on a secrets outage.
# ---------------------------------------------------------------------------


def secrets_outage_client_factory(secrets: FakeSecretsManagerClient, ecs: FakeECSClient):
    def factory(service_name, *args, **kwargs):
        if service_name == "secretsmanager":
            return secrets
        return ecs

    return factory


def test_secrets_outage_non_target_event_returns_200(fake_clickup, fake_ecs, ecs_env, monkeypatch):
    handler._secrets_cache = None
    secrets = FakeSecretsManagerClient()
    secrets.exception = RuntimeError("AccessDeniedException")
    monkeypatch.setattr(handler.boto3, "client", secrets_outage_client_factory(secrets, fake_ecs))
    event = make_event({"event": "taskCreated", "task_id": "abc123", "history_items": []})

    resp = handler.handler(event, None)

    assert resp["statusCode"] == 200
    assert secrets.calls == 0
    assert_no_side_effects(fake_clickup, fake_ecs)


def test_secrets_outage_non_gpbot_tag_update_returns_200(fake_clickup, fake_ecs, ecs_env, monkeypatch):
    handler._secrets_cache = None
    secrets = FakeSecretsManagerClient()
    secrets.exception = RuntimeError("AccessDeniedException")
    monkeypatch.setattr(handler.boto3, "client", secrets_outage_client_factory(secrets, fake_ecs))
    event = make_event(tag_updated_body(tags=("needs-grooming",)))

    resp = handler.handler(event, None)

    assert resp["statusCode"] == 200
    assert secrets.calls == 0
    assert_no_side_effects(fake_clickup, fake_ecs)


def test_invalid_json_body_returns_400(fake_clickup, fake_ecs, ecs_env):
    raw_body = "{not json"
    event = {"headers": {"x-signature": sign(raw_body)}, "body": raw_body}

    resp = handler.handler(event, None)

    assert resp["statusCode"] == 400
    assert fake_clickup.calls == []
    assert fake_ecs.run_task_calls == []


@pytest.mark.parametrize("raw_body", ["[]", "null", "42", '"x"'])
def test_non_dict_json_body_returns_400_without_alarm(fake_clickup, fake_ecs, ecs_env, capsys, raw_body):
    # Valid JSON that is not an object reaches body.get() BEFORE signature
    # verification; it must get the same quiet 400 as malformed JSON, not an
    # AttributeError crash that the Lambda runtime logs as "[ERROR] ..." and
    # the fail-loud alarm picks up (unauthenticated log poisoning).
    event = {"headers": {"x-signature": "junk"}, "body": raw_body}

    resp = handler.handler(event, None)

    assert resp["statusCode"] == 400
    assert fake_clickup.calls == []
    assert fake_ecs.run_task_calls == []
    assert_no_alarm_log_emitted(capsys)


# ---------------------------------------------------------------------------
# 20. Alarm-log poisoning: the endpoint is public, and the metric filter (see
# ALARM_FILTER_TERMS) matches every log line in the log group. The handler must
# never echo unauthenticated (pre-signature-verification) request content to
# its logs — otherwise any internet client can fire the fail-loud alarm, or
# drown it in false positives until ops learns to ignore it.
# ---------------------------------------------------------------------------


def test_skipped_event_type_with_poison_body_logs_nothing_alarmable(fake_clickup, fake_ecs, ecs_env, capsys):
    body = {"event": ALARM_POISON, "task_id": "abc123", "history_items": [], "note": ALARM_POISON}
    event = {"headers": {"x-signature": "junk", "x-note": ALARM_POISON}, "body": json.dumps(body)}

    resp = handler.handler(event, None)

    assert resp["statusCode"] == 200
    assert_no_side_effects(fake_clickup, fake_ecs)
    assert_no_alarm_log_emitted(capsys)


def test_non_target_tag_with_poison_body_logs_nothing_alarmable(fake_clickup, fake_ecs, ecs_env, capsys):
    body = tag_updated_body(tags=(ALARM_POISON,))
    event = {"headers": {"x-signature": "junk"}, "body": json.dumps(body)}

    resp = handler.handler(event, None)

    assert resp["statusCode"] == 200
    assert_no_side_effects(fake_clickup, fake_ecs)
    assert_no_alarm_log_emitted(capsys)


def test_invalid_json_with_poison_body_logs_nothing_alarmable(fake_clickup, fake_ecs, ecs_env, capsys):
    raw_body = f"{ALARM_POISON} {{not json"
    event = {"headers": {"x-signature": "junk"}, "body": raw_body}

    resp = handler.handler(event, None)

    assert resp["statusCode"] == 400
    assert fake_clickup.calls == []
    assert fake_ecs.run_task_calls == []
    assert_no_alarm_log_emitted(capsys)


def test_signature_mismatch_poison_body_is_not_echoed(fake_clickup, fake_ecs, ecs_env, capsys):
    # A gpbot-tagged delivery with a bad signature IS alarm-worthy (rotated
    # secret death mode), but only via the handler's own controlled log line —
    # the attacker-supplied body must never be echoed.
    body = tag_updated_body()
    body["note"] = ALARM_POISON
    event = {"headers": {"x-signature": "0" * 64}, "body": json.dumps(body)}

    resp = handler.handler(event, None)

    assert resp["statusCode"] == 401
    out = assert_alarm_log_emitted(capsys)
    assert "poison" not in out


# ---------------------------------------------------------------------------
# 21. The x-signature value is a shared-secret HMAC that authenticates the
# webhook; if it reaches CloudWatch it is a replayable credential. The handler
# may log the (authenticated) event for debugging, but the signature value
# must be redacted first.
# ---------------------------------------------------------------------------


def test_signature_value_is_redacted_from_logs(fake_clickup, fake_ecs, ecs_env, capsys):
    body = json.dumps(tag_updated_body())
    signature = sign(body)
    event = {"headers": {"x-signature": signature}, "body": body}

    resp = handler.handler(event, None)

    assert resp["statusCode"] == 200
    out = capsys.readouterr().out
    # The raw signature must never appear anywhere in the logs...
    assert signature not in out
    # ...but the event IS logged for debugging, with the value redacted.
    assert "[redacted]" in out


def test_signature_value_is_redacted_from_logs_case_insensitive(fake_clickup, fake_ecs, ecs_env, capsys):
    # ClickUp/API Gateway may deliver the header with any casing; redaction
    # must key off the header name case-insensitively or the secret leaks.
    body = json.dumps(tag_updated_body())
    signature = sign(body)
    event = {"headers": {"X-Signature": signature}, "body": body}

    resp = handler.handler(event, None)

    assert resp["statusCode"] == 200
    assert signature not in capsys.readouterr().out


def test_signature_value_is_redacted_from_multi_value_headers(fake_clickup, fake_ecs, ecs_env, capsys):
    # ALB delivers the signature a SECOND time under multiValueHeaders (values
    # are lists). Redacting only headers still leaks the replayable HMAC through
    # this copy when the (authenticated) event is logged for debugging.
    body = json.dumps(tag_updated_body())
    signature = sign(body)
    event = {
        "headers": {"x-signature": signature},
        "multiValueHeaders": {"x-signature": [signature]},
        "body": body,
    }

    resp = handler.handler(event, None)

    assert resp["statusCode"] == 200
    out = capsys.readouterr().out
    assert signature not in out
    assert "[redacted]" in out


def test_signature_value_is_redacted_from_multi_value_headers_case_insensitive(fake_clickup, fake_ecs, ecs_env, capsys):
    # Same casing hazard as headers: multiValueHeaders may arrive as any casing.
    body = json.dumps(tag_updated_body())
    signature = sign(body)
    event = {
        "headers": {"x-signature": signature},
        "multiValueHeaders": {"X-Signature": [signature]},
        "body": body,
    }

    resp = handler.handler(event, None)

    assert resp["statusCode"] == 200
    assert signature not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 22. Raw boto3 exception text (ARNs, request IDs, credential context) must
# never be posted into a public ClickUp comment. The failure comment names the
# exception type and points at CloudWatch; the full detail stays in the logs.
# ---------------------------------------------------------------------------


def test_run_task_exception_detail_not_leaked_into_comment(fake_clickup, fake_ecs, ecs_env, capsys):
    marker = "arn:aws:iam::123456789012:role/leaked-role reqid=deadbeef-secret"
    fake_ecs.exception = RuntimeError(marker)
    event = make_event(tag_updated_body())

    resp = handler.handler(event, None)

    assert resp["statusCode"] == 500
    failure_comments = [
        text for text in fake_clickup.posted_comment_texts if text.startswith("[GP-Bot] Failed to start processing")
    ]
    assert len(failure_comments) == 1
    comment = failure_comments[0]
    # The leak-prone raw exception message must not reach ClickUp...
    assert marker not in comment
    # ...but the exception class name must, so a human knows what broke.
    assert "RuntimeError" in comment
    # Full detail must still reach the logs (CloudWatch) for debugging.
    assert marker in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 23. Every ClickUp API call authenticates with the configured CLICKUP_API_KEY
# in the Authorization header. Pins the wiring: if the key were dropped or read
# from the wrong secret, ClickUp would 401 and the bot would silently stop
# posting comments and reading task state.
# ---------------------------------------------------------------------------


def test_clickup_requests_carry_authorization_header(fake_clickup, fake_ecs, ecs_env):
    event = make_event(tag_updated_body())

    resp = handler.handler(event, None)

    assert resp["statusCode"] == 200
    # Happy path hits both the GET comments and the POST ack-comment endpoints.
    assert len(fake_clickup.requests) >= 2
    assert all(auth == TEST_API_KEY for auth in fake_clickup.authorization_headers)


# ---------------------------------------------------------------------------
# PR-review findings: pre-auth payload-shape hardening
# (cursor bugbot: null history_items; delegate-reviewer: dict body)
# ---------------------------------------------------------------------------


def test_null_history_items_returns_200_skipped_without_alarmable_log(fake_clickup, fake_ecs, ecs_env, capsys):
    # "history_items": null is present-but-null, so .get(key, []) returns None.
    # Must skip quietly pre-auth, not crash into a runtime [ERROR] log that an
    # unauthenticated client could use to fire the fail-loud alarm.
    body = {"event": "taskTagUpdated", "task_id": "abc123", "history_items": None}
    resp = handler.handler(make_event(body, signature="junk"), None)
    assert resp["statusCode"] == 200
    assert response_body(resp) == {"skipped": "not a target tag"}
    out = capsys.readouterr().out
    assert "ERROR" not in out
    assert "Failed to" not in out


def test_non_dict_history_entries_and_tags_are_skipped(fake_clickup, fake_ecs, ecs_env, capsys):
    # Attacker-shaped entries (strings, numbers, non-dict tags) must not crash
    # pre-auth; a valid entry later in the list must still match.
    history = ["junk-string", 42, {"field": "tag", "after": ["not-a-dict", {"name": "gpbot-analyze"}]}]
    body = tag_updated_body(history_items=history)
    event = make_event(body)
    resp = handler.handler(event, None)
    assert resp["statusCode"] == 200
    assert len(fake_ecs.run_task_calls) == 1
    out = capsys.readouterr().out
    assert "Traceback" not in out


def test_dict_body_direct_invocation_returns_401_not_secrets_error(fake_clickup, fake_ecs, ecs_env, capsys):
    # Direct invocation (console/test) can pass body as an already-parsed dict.
    # Signature can never match a re-serialized dict, so this must be a clean
    # 401 — NOT an AttributeError misclassified as a Secrets Manager outage.
    event = {"headers": {"x-signature": "junk"}, "body": tag_updated_body()}
    resp = handler.handler(event, None)
    assert resp["statusCode"] == 401
    out = capsys.readouterr().out
    assert "Secrets unavailable" not in out
    assert len(fake_ecs.run_task_calls) == 0


def test_ack_post_failure_still_200_but_logs_alarmable_line(fake_clickup, fake_ecs, ecs_env, capsys):
    # If the "Processing started" ack fails AFTER a successful run_task, the
    # Fargate agent is already running: a 500 would make ClickUp re-deliver
    # and guarantee a duplicate launch, so the handler must return 200. The
    # residual risk (a later manual re-tag re-launches because no dedup marker
    # exists) is accepted and made visible: the log line must contain
    # "Failed to" so the CloudWatch metric filter fires the fail-loud alarm.
    fake_clickup.post_comment_error = HTTPError("http://x", 500, "err", {}, None)
    resp = handler.handler(make_event(tag_updated_body()), None)
    assert resp["statusCode"] == 200
    assert len(fake_ecs.run_task_calls) == 1
    assert "Failed to" in capsys.readouterr().out


def test_ecs_failure_reasons_not_leaked_into_comment_or_response(fake_clickup, fake_ecs, ecs_env, capsys):
    # ECS failures[].reason can embed ARNs and account details. The ClickUp
    # comment and HTTP response must carry only a generic message; the raw
    # reasons belong in CloudWatch logs.
    marker = "arn:aws:iam::999999999999:role/secret-leak-marker"
    fake_ecs.response = {"tasks": [], "failures": [{"reason": marker, "detail": "x"}]}
    resp = handler.handler(make_event(tag_updated_body()), None)
    assert resp["statusCode"] == 500
    assert marker not in resp["body"]
    failure_comments = [t for t in fake_clickup.posted_comment_texts if "Failed to start processing" in t]
    assert len(failure_comments) == 1
    assert marker not in failure_comments[0]
    assert marker in capsys.readouterr().out
