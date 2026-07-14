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
from botocore.exceptions import ClientError, ReadTimeoutError

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
        self.post_comment_error = None  # exception to raise on EVERY POST .../comment
        self.post_comment_error_queue = []  # one-shot exceptions, consumed per POST

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
            if self.post_comment_error_queue:
                raise self.post_comment_error_queue.pop(0)
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


class FakeLambdaClient:
    def __init__(self):
        self.invoke_calls = []
        self.exception = None

    def invoke(self, **kwargs):
        self.invoke_calls.append(kwargs)
        if self.exception is not None:
            raise self.exception
        return {"StatusCode": 202}

    @property
    def invoke_payloads(self) -> list[dict]:
        return [json.loads(call["Payload"]) for call in self.invoke_calls]


class FakeDynamoDBClient:
    """Fake for the atomic-dedup DynamoDB client. Records every call.

    put_item_exception / delete_item_exception raise on the corresponding call
    (set put_item_exception to conditional_check_failed() to simulate a lost
    claim race).
    """

    def __init__(self):
        self.put_item_calls = []
        self.delete_item_calls = []
        self.put_item_exception = None
        self.delete_item_exception = None

    def put_item(self, **kwargs):
        self.put_item_calls.append(kwargs)
        if self.put_item_exception is not None:
            raise self.put_item_exception
        return {}

    def delete_item(self, **kwargs):
        self.delete_item_calls.append(kwargs)
        if self.delete_item_exception is not None:
            raise self.delete_item_exception
        return {}


class FakeBoto3ClientFactory:
    """Stands in for boto3.client; routes each service to its fake.

    client_calls records (service_name, kwargs) so tests can pin the botocore
    Config the handler constructs clients with.
    """

    def __init__(
        self,
        ecs_client: FakeECSClient,
        lambda_client: FakeLambdaClient | None = None,
        dynamodb_client: FakeDynamoDBClient | None = None,
    ):
        self.ecs_client = ecs_client
        self.lambda_client = lambda_client if lambda_client is not None else FakeLambdaClient()
        self.dynamodb_client = dynamodb_client if dynamodb_client is not None else FakeDynamoDBClient()
        self.requested_services = []
        self.client_calls = []

    def __call__(self, service_name, *args, **kwargs):
        self.requested_services.append(service_name)
        self.client_calls.append((service_name, kwargs))
        if service_name == "lambda":
            return self.lambda_client
        if service_name == "dynamodb":
            return self.dynamodb_client
        return self.ecs_client


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
def fake_lambda():
    return FakeLambdaClient()


@pytest.fixture(autouse=True)
def fake_dynamodb():
    return FakeDynamoDBClient()


@pytest.fixture(autouse=True)
def boto3_factory(monkeypatch, fake_lambda, fake_dynamodb):
    factory = FakeBoto3ClientFactory(FakeECSClient(), fake_lambda, fake_dynamodb)
    monkeypatch.setattr(handler.boto3, "client", factory)
    return factory


@pytest.fixture(autouse=True)
def fake_ecs(boto3_factory):
    return boto3_factory.ecs_client


@pytest.fixture(autouse=True)
def reset_boto3_client_cache():
    # The handler caches boto3 clients at module level (fast-ack in-path
    # budget); each test monkeypatches boto3.client with its own fakes, so a
    # client cached by one test must never leak into the next.
    handler._lambda_client = None
    handler._ecs_client = None
    yield
    handler._lambda_client = None
    handler._ecs_client = None


@pytest.fixture(autouse=True)
def clean_self_invoke_env(monkeypatch):
    # Tests run outside Lambda, but a developer shell (or the Lambda runtime,
    # which always sets AWS_LAMBDA_FUNCTION_NAME) must not flip tests between
    # the fast-ack and synchronous paths. Default: sync fallback.
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    monkeypatch.delenv("DEDUP_COMMENT_WINDOW_SECONDS", raising=False)


@pytest.fixture(autouse=True)
def clean_dedup_table_env(monkeypatch):
    # Default state is the pre-terraform prod state: no dedup table configured,
    # comment-based dedup only. A DEDUP_TABLE_NAME set in a developer shell must
    # not silently flip the whole suite onto the atomic-dedup path — it gates
    # both the lock functions and the is_atomic_dedup_configured branch point
    # in the async comment-fetch failure handling.
    monkeypatch.delenv("DEDUP_TABLE_NAME", raising=False)
    monkeypatch.delenv("DEDUP_TTL_SECONDS", raising=False)


@pytest.fixture
def dedup_table_env(monkeypatch):
    monkeypatch.setenv("DEDUP_TABLE_NAME", "clickup-bot-dedup-test")
    return "clickup-bot-dedup-test"


@pytest.fixture
def self_invoke_env(monkeypatch):
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "clickup-bot-prod")
    return "clickup-bot-prod"


@pytest.fixture(autouse=True)
def no_ack_retry_delay(monkeypatch):
    # The ack-retry pause's real length is not a behavioral contract and would
    # add wall-clock seconds to every ack-failure test; zero it. raising=False
    # keeps this inert before the constant exists (red phase of its TDD cycle).
    monkeypatch.setattr(handler, "ACK_COMMENT_RETRY_DELAY_SECONDS", 0, raising=False)


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


def test_null_text_item_neither_crashes_dedup_nor_hides_the_marker():
    # ClickUp can ship "text": null on a comment item (JSON null survives
    # item.get("text", "") — the default only covers a MISSING key). The
    # fallback's "".join() then raised TypeError, crashing the whole dedup
    # check mid-webhook. A null item must contribute "" — NOT the string
    # "None" (str-wrapping alone would prepend "None" to the concatenation
    # and silently break the marker prefix match, which this test would
    # catch as a False). So this single assertion pins both contracts:
    # no crash, and null items are invisible to the matcher.
    comments = [
        {
            "id": "90130291038679",
            "comment": [
                {"text": None},
                {"text": "[GP-Bot] Processing started (analyze, model: opus)..."},
            ],
            "user": {"id": 105985359, "username": "Collin Park"},
            "date": epoch_ms_str(PINNED_NOW, 30),
            "reply_count": 0,
        }
    ]
    assert handler.has_processing_started_comment(comments, "analyze", now=PINNED_NOW) is True


def test_non_string_comment_text_neither_crashes_nor_hides_the_marker():
    # ClickUp's top-level comment_text is attacker-of-drift territory: only a
    # null has been observed, but a non-string (int, list, dict) must not
    # crash .startswith() mid-webhook. Anything that isn't a str must fall
    # through to the item-concatenation fallback — which here carries the
    # marker, so the matcher must still block.
    text = "[GP-Bot] Processing started (analyze, model: opus)..."
    comments = [
        {
            "id": "90130291038679",
            "comment_text": 123,
            "comment": [{"text": text}],
            "date": epoch_ms_str(PINNED_NOW, 30),
            "reply_count": 0,
        }
    ]
    assert handler.has_processing_started_comment(comments, "analyze", now=PINNED_NOW) is True


def test_empty_comment_text_falls_through_to_item_concatenation():
    # An empty top-level comment_text carries no information — treating it as
    # authoritative would hide a marker that only lives in the comment[] items.
    # "" must fall through to the fallback, converging with the shared
    # get_text() twin's truthiness semantics (shared/clickup_client.py).
    text = "[GP-Bot] Processing started (analyze, model: opus)..."
    comments = [
        {
            "id": "90130291038679",
            "comment_text": "",
            "comment": [{"text": text}],
            "date": epoch_ms_str(PINNED_NOW, 30),
            "reply_count": 0,
        }
    ]
    assert handler.has_processing_started_comment(comments, "analyze", now=PINNED_NOW) is True


def test_non_string_item_text_contributes_empty_not_str_wrapped():
    # ANY non-string item text (not just null) must contribute "" to the
    # concatenation: str-wrapping 0 to "0" would prepend garbage ahead of the
    # marker and silently break the prefix match — this test fails as False
    # if the fallback ever str()-wraps instead of dropping.
    comments = [
        {
            "id": "90130291038679",
            "comment": [
                {"text": 0},
                {"text": "[GP-Bot] Processing started (analyze, model: opus)..."},
            ],
            "date": epoch_ms_str(PINNED_NOW, 30),
            "reply_count": 0,
        }
    ]
    assert handler.has_processing_started_comment(comments, "analyze", now=PINNED_NOW) is True


def test_recent_analyze_marker_does_not_block_gpbot_work(fake_clickup, fake_ecs, ecs_env):
    # LABEL SCOPE: dedup is per (task, label), mirroring the atomic layer's
    # {task_id}#{label} key. A fresh gpbot-analyze ack marker must NOT
    # suppress a gpbot-work trigger on the same task — analyze-then-implement
    # within 15 minutes is the normal workflow, not a duplicate.
    fake_clickup.comments_response = existing_gpbot_comment_response()  # analyze marker, 30s old
    event = make_event(tag_updated_body(tags=("gpbot-work",)))

    resp = handler.handler(event, None)

    assert resp["statusCode"] == 200
    assert response_body(resp)["fargate_task_arn"] == TASK_ARN
    assert len(fake_ecs.run_task_calls) == 1


def test_recent_same_label_marker_still_blocks_at_unit_level():
    # The label-scoped prefix must still match its OWN label's marker.
    comments = processing_started_comments(epoch_ms_str(PINNED_NOW, 30))
    assert handler.has_processing_started_comment(comments, "analyze", now=PINNED_NOW) is True
    assert handler.has_processing_started_comment(comments, "implement", now=PINNED_NOW) is False


# ---------------------------------------------------------------------------
# 9b. Dedup recency window: a "Processing started" marker only blocks while it
# is RECENT (DEDUP_COMMENT_WINDOW_SECONDS, default 900). The marker exists to
# absorb webhook retry storms and double-tags (seconds-to-minutes); a human
# re-tagging a task hours later to deliberately re-run must not be silently
# ignored. (Dedup never actually fired before the 2026-07-14 fix, so
# "re-tag always re-runs" is the behavior users know; an unbounded marker
# would silently change it.)
# ---------------------------------------------------------------------------

PINNED_NOW = 1784040000.0  # arbitrary fixed epoch so boundary math is exact


def processing_started_comments(date_value) -> list[dict]:
    """Real-shape comment list whose marker carries an explicit raw date value."""
    text = "[GP-Bot] Processing started (analyze, model: opus)..."
    comment = {
        "id": "90130291038679",
        "comment": [{"text": text}],
        "comment_text": text,
        "user": {"id": 105985359, "username": "Collin Park"},
        "reply_count": 0,
    }
    if date_value is not None:
        comment["date"] = date_value
    return [comment]


def epoch_ms_str(now: float, age_seconds: float) -> str:
    return str(int((now - age_seconds) * 1000))


def test_marker_899s_old_blocks_at_default_window():
    comments = processing_started_comments(epoch_ms_str(PINNED_NOW, 899))
    assert handler.has_processing_started_comment(comments, "analyze", now=PINNED_NOW) is True


def test_marker_901s_old_does_not_block_at_default_window():
    comments = processing_started_comments(epoch_ms_str(PINNED_NOW, 901))
    assert handler.has_processing_started_comment(comments, "analyze", now=PINNED_NOW) is False


def test_marker_with_missing_date_does_not_block_and_alarms(capsys):
    # An undatable marker must NOT block: blocking would have no age bound, so
    # a ClickUp date-format drift would silently and permanently disable
    # re-tag re-runs. Failing toward duplicate risk is bounded (the atomic
    # DynamoDB layer still guards duplicates), and the shape drift is an
    # integration break an operator must see — the line is alarm-matching.
    comments = processing_started_comments(None)
    assert handler.has_processing_started_comment(comments, "analyze", now=PINNED_NOW) is False
    out = assert_alarm_log_emitted(capsys)
    assert "date unparseable" in out


def test_marker_with_unparseable_date_does_not_block_and_alarms(capsys):
    comments = processing_started_comments("not-a-number")
    assert handler.has_processing_started_comment(comments, "analyze", now=PINNED_NOW) is False
    out = assert_alarm_log_emitted(capsys)
    assert "date unparseable" in out


def test_marker_59s_in_the_future_still_blocks_within_skew_tolerance():
    # Sub-minute NEGATIVE ages are expected in normal operation: the age is
    # OUR clock minus CLICKUP's clock, so the bot's own just-posted ack read
    # back through a ClickUp server whose clock runs slightly ahead shows up
    # seconds "in the future". That marker is the exact retry-storm marker the
    # dedup window exists for — it MUST still block.
    comments = processing_started_comments(epoch_ms_str(PINNED_NOW, -59))
    assert handler.has_processing_started_comment(comments, "analyze", now=PINNED_NOW) is True


def test_marker_exactly_at_skew_tolerance_boundary_blocks():
    # Boundary is inclusive: age == -CLOCK_SKEW_TOLERANCE_SECONDS still blocks.
    comments = processing_started_comments(epoch_ms_str(PINNED_NOW, -60))
    assert handler.has_processing_started_comment(comments, "analyze", now=PINNED_NOW) is True


def test_marker_61s_in_the_future_does_not_block_and_alarms(capsys):
    # Beyond the skew tolerance the date is clock/shape drift, and the window
    # cannot be evaluated. Before the skew guard, ANY future date made
    # age_seconds negative and `age <= window` trivially True — drift would
    # BLOCK re-triggering for skew+window, inverting the documented
    # fail-toward-not-blocking posture of every other undatable-marker case.
    # Like the unparseable-date case, this is an integration break an operator
    # must see: the line is alarm-matching.
    comments = processing_started_comments(epoch_ms_str(PINNED_NOW, -61))
    assert handler.has_processing_started_comment(comments, "analyze", now=PINNED_NOW) is False
    out = assert_alarm_log_emitted(capsys)
    assert "date is in the future" in out


@pytest.mark.parametrize("bad_value", ["nan", "inf", "-inf", "0", "-60", "junk"])
def test_non_finite_or_non_positive_window_falls_back_to_default_quietly(monkeypatch, capsys, bad_value):
    # float() happily parses 'nan'/'inf'/'-inf', which escape a bare
    # ValueError guard: NaN comparisons are always False (window silently
    # disabled) and inf breaks later arithmetic. Any non-finite or
    # non-positive value must fall back to the default with the existing
    # quiet (non-alarm) line — this runs in-path on every dedup check.
    monkeypatch.setenv("DEDUP_COMMENT_WINDOW_SECONDS", bad_value)

    assert handler.get_dedup_window_seconds() == handler.DEFAULT_DEDUP_COMMENT_WINDOW_SECONDS

    out = assert_no_alarm_log_emitted(capsys)
    assert "Invalid DEDUP_COMMENT_WINDOW_SECONDS" in out


def test_window_is_configurable_via_env(monkeypatch):
    monkeypatch.setenv("DEDUP_COMMENT_WINDOW_SECONDS", "60")
    recent = processing_started_comments(epoch_ms_str(PINNED_NOW, 30))
    stale = processing_started_comments(epoch_ms_str(PINNED_NOW, 120))
    assert handler.has_processing_started_comment(recent, "analyze", now=PINNED_NOW) is True
    assert handler.has_processing_started_comment(stale, "analyze", now=PINNED_NOW) is False


def incident_shaped_comments(marker_ages_seconds: list[float], now: float) -> list[dict]:
    """Comment list mirroring the REAL captured DATA-2108 incident payload
    (13 comments): one human comment whose items carry "type": "tag" user
    mentions and "attributes", six non-marker bot analysis comments, and six
    'Processing started' markers — newest first, string epoch-ms dates, no
    "type" key on plain text items. Marker ages are injectable so tests can
    place them inside/outside the dedup window.
    """
    marker_text = "[GP-Bot] Processing started (analyze, model: opus)..."
    comments: list[dict] = [
        {
            "id": "90130291057993",
            "comment": [
                {"type": "tag", "user": {"id": 105985351, "username": "Hugh Karimi"}, "text": "@Hugh Karimi"},
                {
                    "text": " BR's data is out of date here. Anything you need before you can escalate?",
                    "attributes": {},
                },
                {"text": "\n", "attributes": {"block-id": "block-42H9Q2cBDM"}},
            ],
            "comment_text": "@Hugh Karimi BR's data is out of date here. Anything you need before you can escalate?\n",
            "user": {"id": 111932291, "username": "Chadwyck"},
            "date": epoch_ms_str(now, 10),
            "reply_count": 0,
        }
    ]
    analysis_texts = [
        "[GP-Bot] Analysis\n\n## Reproduction / verified facts\n- Confirmed the customer-reported 83k voter count",
        "[GP-Bot] Analysis — reproduces the report, root cause is upstream data",
        "[GP-Bot] Analysis\n\n**Reproduced.** The 83k voter count is real",
        "[GP-Bot] **Analysis — DATA-2108 / Montebello Unified School Board**",
        "[GP-Bot] Re-verified — findings unchanged from prior investigation",
        "[GP-Bot] Investigation summary — Montebello USD school-board race",
    ]
    for i, text in enumerate(analysis_texts):
        comments.append(
            {
                "id": f"9013029104{i:04d}",
                "comment": [{"text": text}],
                "comment_text": text,
                "user": {"id": 105985359, "username": "Collin Park"},
                "date": epoch_ms_str(now, 60 + i),
                "reply_count": 0,
            }
        )
    for i, age in enumerate(marker_ages_seconds):
        comments.append(
            {
                "id": f"9013029105{i:04d}",
                "comment": [{"text": marker_text}],
                "comment_text": marker_text,
                "user": {"id": 105985359, "username": "Collin Park"},
                "date": epoch_ms_str(now, age),
                "reply_count": 0,
            }
        )
    return comments


def test_incident_payload_recent_same_label_marker_blocks():
    # Realistic 13-comment payload (real captured incident shape): human
    # comment, six bot analyses, six markers — one marker inside the window.
    # The matcher must pick the marker out of the noise and block analyze,
    # while implement (different label) stays unblocked.
    ages = [30.0, 200.0, 400.0, 1000.0, 2000.0, 3600.0]
    comments = incident_shaped_comments(ages, PINNED_NOW)

    assert handler.has_processing_started_comment(comments, "analyze", now=PINNED_NOW) is True
    assert handler.has_processing_started_comment(comments, "implement", now=PINNED_NOW) is False


def test_incident_payload_only_stale_markers_do_not_block():
    # All six markers older than the window: analysis comments and the human
    # comment must not be mistaken for markers, so nothing blocks.
    ages = [1000.0, 1200.0, 1800.0, 2400.0, 3000.0, 3600.0]
    comments = incident_shaped_comments(ages, PINNED_NOW)

    assert handler.has_processing_started_comment(comments, "analyze", now=PINNED_NOW) is False


def test_ack_comment_mentions_retag_cooldown(fake_clickup, fake_ecs, ecs_env):
    # UX: a deliberate re-tag inside the dedup window is suppressed with zero
    # feedback — the ack comment is the only place users can learn the
    # cooldown exists. The number derives from the configured window.
    resp = handler.handler(make_event(tag_updated_body()), None)

    assert resp["statusCode"] == 200
    ack = next(t for t in fake_clickup.posted_comment_texts if t.startswith("[GP-Bot] Processing started"))
    assert "(re-tag after 15 minutes to re-run)" in ack


def test_ack_cooldown_minutes_derive_from_configured_window(fake_clickup, fake_ecs, ecs_env, monkeypatch):
    monkeypatch.setenv("DEDUP_COMMENT_WINDOW_SECONDS", "600")

    resp = handler.handler(make_event(tag_updated_body()), None)

    assert resp["statusCode"] == 200
    ack = next(t for t in fake_clickup.posted_comment_texts if t.startswith("[GP-Bot] Processing started"))
    assert "(re-tag after 10 minutes to re-run)" in ack


def test_ack_cooldown_minutes_round_up_never_understate(fake_clickup, fake_ecs, ecs_env, monkeypatch):
    # The hint must never promise an earlier re-run than the window enforces:
    # round(89/60) would say "1 minute" while the marker still blocks at 89s.
    # Ceil is the only rounding that keeps the promise honest.
    monkeypatch.setenv("DEDUP_COMMENT_WINDOW_SECONDS", "89")

    resp = handler.handler(make_event(tag_updated_body()), None)

    assert resp["statusCode"] == 200
    ack = next(t for t in fake_clickup.posted_comment_texts if t.startswith("[GP-Bot] Processing started"))
    assert "(re-tag after 2 minutes to re-run)" in ack


def test_full_ack_text_with_suffix_matches_label_scoped_matcher(fake_clickup, fake_ecs, ecs_env):
    # Round-trip: the exact text the handler posts (including the cooldown
    # suffix) must still be recognized by the dedup matcher — the matcher
    # matches the label-scoped PREFIX, so appending is safe. This test breaks
    # if anyone ever inserts text between the label and the prefix.
    handler.handler(make_event(tag_updated_body()), None)
    ack_text = next(t for t in fake_clickup.posted_comment_texts if t.startswith("[GP-Bot] Processing started"))

    comments = [
        {
            "id": "90130291038679",
            "comment": [{"text": ack_text}],
            "comment_text": ack_text,
            "date": epoch_ms_str(PINNED_NOW, 30),
            "reply_count": 0,
        }
    ]
    assert handler.has_processing_started_comment(comments, "analyze", now=PINNED_NOW) is True
    assert handler.has_processing_started_comment(comments, "implement", now=PINNED_NOW) is False


def test_stale_marker_allows_deliberate_re_tag_to_retrigger(fake_clickup, fake_ecs, ecs_env):
    # Handler-level: a marker from hours ago must NOT block a fresh tag event —
    # a human re-tagging later is a deliberate re-run, not a retry storm.
    fake_clickup.comments_response = existing_gpbot_comment_response(age_seconds=3600)
    event = make_event(tag_updated_body())

    resp = handler.handler(event, None)

    assert resp["statusCode"] == 200
    assert response_body(resp)["fargate_task_arn"] == TASK_ARN
    assert len(fake_ecs.run_task_calls) == 1


# ---------------------------------------------------------------------------
# 9c. Fast-ack via async self-invoke. ClickUp's webhook delivery has a short
# response timeout; in the 2026-07-14 incident every invocation ran 7.6-20.5s
# of serial ClickUp API calls, every delivery timed out, and ClickUp retried
# one event 5x (6 Fargate launches) while counting each timeout toward the
# ~100 consecutive failures that auto-disable the webhook. The handler must
# therefore 200 BEFORE any ClickUp API call: it self-invokes asynchronously
# and the worker invocation does dedup + trigger + ack off the critical path.
# Until the self-invoke IAM lands (separate terraform PR), the invoke fails
# and the handler must fall back to exactly the old synchronous flow.
# ---------------------------------------------------------------------------


def test_fast_ack_returns_accepted_and_self_invokes(fake_clickup, fake_ecs, fake_lambda, ecs_env, self_invoke_env):
    event = make_event(tag_updated_body())

    resp = handler.handler(event, None)

    assert resp["statusCode"] == 200
    assert response_body(resp)["status"] == "accepted"
    # Exactly one async self-invoke carrying the validated work item.
    assert len(fake_lambda.invoke_calls) == 1
    call = fake_lambda.invoke_calls[0]
    assert call["FunctionName"] == self_invoke_env
    assert call["InvocationType"] == "Event"
    assert fake_lambda.invoke_payloads[0] == {
        "gpbot_async": True,
        "task_id": "abc123",
        "matched_tag": "gpbot-analyze",
    }
    # THE point of fast-ack: zero ClickUp API calls and zero ECS calls in-path.
    assert fake_clickup.calls == []
    assert fake_ecs.run_task_calls == []


def test_missing_function_name_env_falls_back_to_sync_quietly(fake_clickup, fake_ecs, fake_lambda, ecs_env, capsys):
    # clean_self_invoke_env (autouse) removed AWS_LAMBDA_FUNCTION_NAME: the
    # handler must not attempt an invoke and must run the full synchronous
    # flow, logging only a quiet (non-alarm) line about the fallback.
    event = make_event(tag_updated_body())

    resp = handler.handler(event, None)

    assert resp["statusCode"] == 200
    assert response_body(resp)["fargate_task_arn"] == TASK_ARN
    assert fake_lambda.invoke_calls == []
    assert len(fake_ecs.run_task_calls) == 1
    assert any(text.startswith("[GP-Bot] Processing started") for text in fake_clickup.posted_comment_texts)
    out = assert_no_alarm_log_emitted(capsys)
    assert "Async self-invoke unavailable" in out


def make_invoke_client_error(code: str, message: str = "denied") -> ClientError:
    """Real botocore ClientError shape for lambda.invoke failures."""
    return ClientError({"Error": {"Code": code, "Message": message}}, "Invoke")


def test_invoke_access_denied_falls_back_to_full_sync_flow(
    fake_clickup, fake_ecs, fake_lambda, ecs_env, self_invoke_env, capsys
):
    # Initial prod state: the self-invoke IAM permission ships in a later
    # terraform PR, so lambda:InvokeFunction is denied. AccessDenied is a
    # DETERMINISTIC "the Event was NOT accepted" rejection, so the fallback
    # must preserve exactly today's synchronous behavior (dedup GET +
    # run_task + ack) and the fallback itself must NOT fire the alarm — it
    # is expected on every delivery until the IAM lands.
    fake_lambda.exception = make_invoke_client_error(
        "AccessDeniedException", "not authorized to perform lambda:InvokeFunction"
    )
    event = make_event(tag_updated_body())

    resp = handler.handler(event, None)

    assert resp["statusCode"] == 200
    assert response_body(resp)["fargate_task_arn"] == TASK_ARN
    assert len(fake_lambda.invoke_calls) == 1
    # Dedup GET ran...
    assert any(method == "GET" and "/comment" in url for method, url, _ in fake_clickup.calls)
    # ...the task launched, and the ack comment was posted.
    assert len(fake_ecs.run_task_calls) == 1
    assert any(text.startswith("[GP-Bot] Processing started") for text in fake_clickup.posted_comment_texts)
    assert_no_alarm_log_emitted(capsys)


def test_deterministic_invoke_rejection_with_alarm_terms_in_message_stays_quiet(
    fake_clickup, fake_ecs, fake_lambda, ecs_env, self_invoke_env, capsys
):
    # The sync-fallback line fires on EVERY delivery until the self-invoke IAM
    # lands, and raw botocore messages can contain alarm-filter terms — e.g.
    # "Failed to connect to endpoint". Echoing the message would fire the
    # fail-loud alarm on every single delivery of the initial prod state.
    # Only the exception type / error code may be logged (same pattern as the
    # ack first-failure line).
    fake_lambda.exception = make_invoke_client_error("AccessDeniedException", "ERROR: Failed to invoke lambda")
    event = make_event(tag_updated_body())

    resp = handler.handler(event, None)

    assert resp["statusCode"] == 200
    assert response_body(resp)["fargate_task_arn"] == TASK_ARN
    out = assert_no_alarm_log_emitted(capsys)
    assert "Async self-invoke unavailable" in out
    assert "AccessDeniedException" in out


def test_ambiguous_invoke_read_timeout_returns_500_without_sync_processing(
    fake_clickup, fake_ecs, fake_lambda, ecs_env, self_invoke_env, capsys
):
    # DOUBLE-LAUNCH GUARD: an async Event invoke can fail CLIENT-side after
    # being accepted SERVER-side (read timeout, dropped connection) — the
    # worker may already be queued. Falling back would then run BOTH the
    # worker and the inline path for one delivery. Ambiguous failures must
    # NOT fall back: 500 (ClickUp redelivers; the dedup layers absorb the
    # possible duplicate worker), zero ClickUp API calls, zero ECS launches,
    # and an alarm-matching line — this is a genuine failure, unlike the
    # expected deterministic rejection above.
    fake_lambda.exception = ReadTimeoutError(endpoint_url="https://lambda.us-west-2.amazonaws.com/")
    event = make_event(tag_updated_body())

    resp = handler.handler(event, None)

    assert resp["statusCode"] == 500
    assert fake_clickup.calls == []
    assert fake_ecs.run_task_calls == []
    out = assert_alarm_log_emitted(capsys)
    assert "Failed to enqueue async processing: ReadTimeoutError" in out
    # Type only, never the raw message (the endpoint URL adds nothing and raw
    # botocore messages are what the quiet deterministic path guards against).
    assert "lambda.us-west-2.amazonaws.com" not in out


@pytest.mark.parametrize(
    "code",
    [
        "AccessDeniedException",
        "ResourceNotFoundException",
        "InvalidParameterValueException",
        "UnrecognizedClientException",
    ],
)
def test_enqueue_returns_fallback_for_deterministic_rejection_codes(fake_lambda, self_invoke_env, code):
    # Unit pin of the determinism split: these codes mean the control plane
    # REFUSED the Event (never queued), so inline processing cannot double-run.
    fake_lambda.exception = make_invoke_client_error(code)
    assert handler.enqueue_async_processing("abc123", "gpbot-analyze") == "fallback"


@pytest.mark.parametrize(
    "exception",
    [
        make_invoke_client_error("TooManyRequestsException", "Rate exceeded"),  # throttle: retry may have landed
        make_invoke_client_error("ServiceException", "internal error"),  # unknown code: can't prove not-queued
        ReadTimeoutError(endpoint_url="https://lambda.us-west-2.amazonaws.com/"),
        ConnectionResetError("connection reset by peer"),
    ],
)
def test_enqueue_returns_ambiguous_for_everything_else(fake_lambda, self_invoke_env, capsys, exception):
    # Any failure that cannot PROVE the Event was rejected is ambiguous — the
    # invoke may have been accepted server-side before the client gave up.
    fake_lambda.exception = exception
    assert handler.enqueue_async_processing("abc123", "gpbot-analyze") == "ambiguous"
    assert_alarm_log_emitted(capsys)


def test_enqueue_returns_accepted_on_success(fake_lambda, self_invoke_env):
    assert handler.enqueue_async_processing("abc123", "gpbot-analyze") == "accepted"


def test_enqueue_returns_fallback_when_function_name_missing(fake_lambda):
    # clean_self_invoke_env (autouse) removed AWS_LAMBDA_FUNCTION_NAME:
    # deterministic — no invoke was even attempted, nothing can be queued.
    assert handler.enqueue_async_processing("abc123", "gpbot-analyze") == "fallback"
    assert fake_lambda.invoke_calls == []


def test_boto3_clients_are_cached_across_invocations(
    fake_clickup, fake_ecs, fake_lambda, boto3_factory, ecs_env, self_invoke_env
):
    # boto3.client() re-runs endpoint resolution and session wiring on every
    # call — in-path latency against the fast-ack promise of an instant 200.
    # Each service client must be constructed once per execution environment
    # and reused across invocations (warm Lambda containers).
    handler.handler(make_event(tag_updated_body()), None)
    handler.handler(make_event(tag_updated_body()), None)

    assert boto3_factory.requested_services.count("lambda") == 1


def test_ecs_client_is_cached_across_invocations(fake_clickup, fake_ecs, boto3_factory, ecs_env):
    # Same caching contract for the ECS client used by the sync fallback /
    # async worker.
    handler.handler(make_event(tag_updated_body()), None)
    handler.handler(make_event(tag_updated_body()), None)

    assert boto3_factory.requested_services.count("ecs") == 1


def test_lambda_client_uses_fast_ack_timeouts(fake_clickup, fake_ecs, boto3_factory, ecs_env, self_invoke_env):
    # botocore defaults are 60s connect + 60s read with retries: a hung Lambda
    # control plane would blow ClickUp's whole webhook timeout in-path. The
    # fast-ack budget demands tight timeouts and a single attempt — a provable
    # rejection takes the quiet sync fallback, anything ambiguous 500s so
    # ClickUp redelivers (no inline double-run). total_max_attempts, NOT
    # max_attempts: legacy-mode botocore reads max_attempts as retries AFTER
    # the initial call, so {"max_attempts": 1} silently means 2 attempts (see
    # test_lambda_client_config_resolves_to_a_single_total_attempt).
    handler.handler(make_event(tag_updated_body()), None)

    lambda_calls = [kwargs for service, kwargs in boto3_factory.client_calls if service == "lambda"]
    assert len(lambda_calls) == 1
    config = lambda_calls[0]["config"]
    assert config.connect_timeout == 2
    assert config.read_timeout == 5
    assert config.retries == {"total_max_attempts": 1}


def test_lambda_client_config_resolves_to_a_single_total_attempt():
    # BEHAVIORAL, not just a dict pin: build a REAL botocore client from the
    # handler's constant and assert the RESOLVED retry budget. botocore's
    # legacy mode normalizes retries into {"total_max_attempts": N, "mode":
    # "legacy"} where N counts the initial call — the previous
    # {"max_attempts": 1} config resolved to total_max_attempts=2 (one full
    # SDK retry on the fast-ack path), which this test would have caught.
    # The fakes intercept boto3.client (not the transport), so the module-
    # level boto3.session path below deliberately bypasses the patched
    # boto3.client to reach real botocore config resolution. No network I/O:
    # client construction only resolves endpoints/config locally.
    import boto3.session

    real_client = boto3.session.Session(
        region_name="us-west-2",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    ).client("lambda", config=handler.LAMBDA_CLIENT_CONFIG)

    assert real_client.meta.config.retries["total_max_attempts"] == 1


def test_async_event_runs_dedup_and_triggers(fake_clickup, fake_ecs, fake_lambda, ecs_env):
    event = {"gpbot_async": True, "task_id": "abc123", "matched_tag": "gpbot-analyze"}

    resp = handler.handler(event, None)

    assert resp["statusCode"] == 200
    assert response_body(resp)["fargate_task_arn"] == TASK_ARN
    # The worker owns the ClickUp work: dedup GET, launch, ack comment.
    assert any(method == "GET" and "/comment" in url for method, url, _ in fake_clickup.calls)
    assert len(fake_ecs.run_task_calls) == 1
    assert any(text.startswith("[GP-Bot] Processing started") for text in fake_clickup.posted_comment_texts)
    # The worker must not re-enqueue itself.
    assert fake_lambda.invoke_calls == []


def test_async_event_with_recent_marker_skips(fake_clickup, fake_ecs, ecs_env):
    # The retry-storm case the whole design exists for: a duplicate delivery's
    # worker sees the first worker's recent ack comment and stops.
    fake_clickup.comments_response = existing_gpbot_comment_response()
    event = {"gpbot_async": True, "task_id": "abc123", "matched_tag": "gpbot-analyze"}

    resp = handler.handler(event, None)

    assert resp["statusCode"] == 200
    assert response_body(resp) == {"skipped": "already processed"}
    assert fake_ecs.run_task_calls == []
    assert fake_clickup.posted_comments == []


def test_async_worker_exception_returns_dict_and_alarms(fake_clickup, fake_ecs, ecs_env, monkeypatch, capsys):
    # An unhandled exception in an async ("Event") invocation makes Lambda
    # auto-RETRY it — recreating exactly the duplicate-launch bug this design
    # fixes. The worker must ALWAYS return a dict; the alarm-matching ERROR
    # log is the only fail-loud channel (nobody receives an HTTP error), and
    # a best-effort failure comment tells the tagger.
    def explode(task_id, matched_tag):
        raise RuntimeError("unexpected worker crash")

    monkeypatch.setattr(handler, "dedup_check_then_trigger", explode)
    event = {"gpbot_async": True, "task_id": "abc123", "matched_tag": "gpbot-analyze"}

    resp = handler.handler(event, None)

    assert isinstance(resp, dict)
    assert resp["statusCode"] == 500
    out = assert_alarm_log_emitted(capsys)
    assert "ERROR: Async processing failed" in out
    failure_comments = [
        text for text in fake_clickup.posted_comment_texts if text.startswith("[GP-Bot] Failed to start processing")
    ]
    assert len(failure_comments) == 1


def test_async_event_with_unknown_tag_returns_dict_without_side_effects(fake_clickup, fake_ecs, ecs_env, capsys):
    # Defensive: the payload is self-generated, so an unknown tag means a bug
    # (or a direct invoke with AWS creds) — refuse loudly, never launch.
    event = {"gpbot_async": True, "task_id": "abc123", "matched_tag": "not-a-tag"}

    resp = handler.handler(event, None)

    assert isinstance(resp, dict)
    assert resp["statusCode"] == 400
    assert fake_ecs.run_task_calls == []
    assert fake_clickup.calls == []
    assert_alarm_log_emitted(capsys)


def test_alb_body_gpbot_async_is_not_async_dispatch(
    fake_clickup, fake_ecs, fake_lambda, ecs_env, self_invoke_env, capsys
):
    # SPOOF GUARD: an attacker POSTing {"gpbot_async": true, ...} through the
    # ALB lands in event["body"] as a string — top-level event keys are
    # unspoofable. The delivery must be treated as a normal webhook (signature
    # verified, 401 on mismatch), never dispatched to the trusted worker path.
    body = tag_updated_body()
    body["gpbot_async"] = True
    event = {"headers": {"x-signature": "0" * 64}, "body": json.dumps(body)}

    resp = handler.handler(event, None)

    assert resp["statusCode"] == 401
    assert fake_clickup.calls == []
    assert fake_ecs.run_task_calls == []
    assert fake_lambda.invoke_calls == []
    assert_alarm_log_emitted(capsys)  # signature-mismatch 401s must still alarm


def test_top_level_gpbot_async_with_alb_keys_is_not_async_dispatch(fake_clickup, fake_ecs, fake_lambda, ecs_env):
    # Belt-and-braces for the same guard: even a top-level gpbot_async key is
    # ignored when the event carries ALB markers (headers/requestContext) —
    # an ALB-wrapped request always has those, an internal self-invoke never does.
    body = tag_updated_body()
    event = make_event(body)  # valid signature
    event["gpbot_async"] = True
    event["requestContext"] = {"elb": {"targetGroupArn": "arn:aws:elasticloadbalancing:..."}}

    resp = handler.handler(event, None)

    # Processed as a normal webhook: with no AWS_LAMBDA_FUNCTION_NAME... but
    # self_invoke fixture not used here, so sync flow runs end-to-end.
    assert resp["statusCode"] == 200
    assert response_body(resp)["fargate_task_arn"] == TASK_ARN
    assert len(fake_ecs.run_task_calls) == 1
    assert fake_lambda.invoke_calls == []


# ---------------------------------------------------------------------------
# 10. Comment fetch failure -> 500
# ---------------------------------------------------------------------------


def test_comment_fetch_http_error_returns_500(fake_clickup, fake_ecs, ecs_env, capsys):
    # SYNC path pinned: ClickUp receives the 500 and redelivers, so failing
    # the delivery is self-healing at-least-once — keep exactly as is.
    fake_clickup.get_comments_error = HTTPError("http://x", 500, "err", {}, None)
    event = make_event(tag_updated_body())

    resp = handler.handler(event, None)

    assert resp["statusCode"] == 500
    assert fake_ecs.run_task_calls == []
    assert_alarm_log_emitted(capsys)


def test_async_comment_fetch_failure_with_atomic_backstop_still_launches(
    fake_clickup, fake_ecs, ecs_env, monkeypatch, capsys
):
    # ASYNC path + DEDUP_TABLE_NAME configured: ClickUp already got its 200
    # 'accepted', so a 500 dict returned here goes NOWHERE — the tag event
    # would be permanently dropped. The comment layer is best-effort (the
    # atomic conditional write still guards duplicates), so a comment-fetch
    # failure must skip the best-effort check and PROCEED to launch. The
    # degraded state is alarm-worthy: 'Failed to get comments' must fire.
    monkeypatch.setenv("DEDUP_TABLE_NAME", "clickup-bot-dedup-test")
    fake_clickup.get_comments_error = HTTPError("http://x", 500, "err", {}, None)

    resp = handler.handler(async_worker_event(), None)

    assert resp["statusCode"] == 200
    assert response_body(resp)["fargate_task_arn"] == TASK_ARN
    assert len(fake_ecs.run_task_calls) == 1
    out = assert_alarm_log_emitted(capsys)
    assert "Failed to get comments" in out


def test_async_comment_fetch_failure_without_backstop_posts_failure_comment(fake_clickup, fake_ecs, ecs_env, capsys):
    # ASYNC path, NO atomic backstop configured: launching blind would be
    # unbounded duplicate risk, and returning a 500 dict would silently drop
    # the delivery. The user must get a visible retry path instead: the
    # standard failure comment ('Remove and re-add the tag to retry.'), no
    # launch, no raise.
    fake_clickup.get_comments_error = URLError("connection refused")

    resp = handler.handler(async_worker_event(), None)

    assert isinstance(resp, dict)
    assert resp["statusCode"] == 500
    assert fake_ecs.run_task_calls == []
    failure_comments = [
        text for text in fake_clickup.posted_comment_texts if text.startswith("[GP-Bot] Failed to start processing")
    ]
    assert len(failure_comments) == 1
    assert "Remove and re-add the tag to retry" in failure_comments[0]
    assert_alarm_log_emitted(capsys)


def test_is_atomic_dedup_configured_reads_dedup_table_env(monkeypatch):
    # Unit-level pin for the branch-point predicate: in this PR the env var is
    # never set in prod (the atomic layer ships in the stacked PR), so the
    # configured arm is dead code at the handler level — the predicate itself
    # must still be locked down.
    monkeypatch.delenv("DEDUP_TABLE_NAME", raising=False)
    assert handler.is_atomic_dedup_configured() is False
    monkeypatch.setenv("DEDUP_TABLE_NAME", "")
    assert handler.is_atomic_dedup_configured() is False
    monkeypatch.setenv("DEDUP_TABLE_NAME", "clickup-bot-dedup-prod")
    assert handler.is_atomic_dedup_configured() is True


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


def async_worker_event(task_id: str = "abc123", matched_tag: str = "gpbot-analyze") -> dict:
    """Trusted internal payload the fast-ack path self-invokes with."""
    return {"gpbot_async": True, "task_id": task_id, "matched_tag": matched_tag}


def test_async_ack_transient_failure_is_retried_and_succeeds(fake_clickup, fake_ecs, ecs_env, capsys):
    # The ack comment is the dedup marker: without it, the retry-storm window
    # stays open. In the ASYNC WORKER, ClickUp already got its 200, so this
    # post is off the webhook critical path: a transient ClickUp 5xx on the
    # first attempt must be retried once — and a successful retry is a
    # non-event: no alarm.
    fake_clickup.post_comment_error_queue = [HTTPError("http://x", 502, "bad gateway", {}, None)]

    resp = handler.handler(async_worker_event(), None)

    assert resp["statusCode"] == 200
    assert response_body(resp)["fargate_task_arn"] == TASK_ARN
    assert len(fake_ecs.run_task_calls) == 1
    # Two POST attempts: the failed one and the successful retry.
    assert len(fake_clickup.posted_comments) == 2
    assert fake_clickup.posted_comments[-1]["comment_text"].startswith("[GP-Bot] Processing started")
    assert_no_alarm_log_emitted(capsys)


def test_async_ack_double_failure_alarms_after_second_attempt(fake_clickup, fake_ecs, ecs_env, capsys):
    # Only the SECOND failure logs the alarm-matching line — swallowed ack
    # failures are exactly what the alarm exists for (it alarmed correctly
    # during the 2026-07-14 incident). Exactly two attempts: unbounded retries
    # against a down ClickUp would just burn the worker invocation.
    fake_clickup.post_comment_error = HTTPError("http://x", 500, "err", {}, None)

    resp = handler.handler(async_worker_event(), None)

    assert resp["statusCode"] == 200
    assert len(fake_ecs.run_task_calls) == 1
    assert len(fake_clickup.posted_comments) == 2
    out = assert_alarm_log_emitted(capsys)
    assert "Failed to post starting comment" in out


def test_sync_fallback_ack_failure_posts_once_without_sleep(fake_clickup, fake_ecs, ecs_env, capsys, monkeypatch):
    # The SYNC FALLBACK is the guaranteed initial prod state (self-invoke IAM
    # ships in a later terraform PR) and runs while ClickUp is still waiting
    # on the webhook response — the exact path whose slowness caused the
    # 2026-07-14 retry storm. The ack must be a single attempt with NO sleep
    # and NO retry: adding up to 12s here would guarantee webhook timeouts.
    sleep_calls: list[float] = []
    monkeypatch.setattr(handler.time, "sleep", lambda seconds: sleep_calls.append(seconds))
    fake_clickup.post_comment_error = HTTPError("http://x", 500, "err", {}, None)

    resp = handler.handler(make_event(tag_updated_body()), None)

    assert resp["statusCode"] == 200
    assert len(fake_ecs.run_task_calls) == 1
    started_posts = [t for t in fake_clickup.posted_comment_texts if t.startswith("[GP-Bot] Processing started")]
    assert len(started_posts) == 1
    assert sleep_calls == []
    # The swallowed ack failure is still alarm-worthy (missing dedup marker).
    out = assert_alarm_log_emitted(capsys)
    assert "Failed to post starting comment" in out


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


# ---------------------------------------------------------------------------
# 24. Atomic dedup lock (DynamoDB conditional write). Comment-based dedup is
# best-effort: it reads through ClickUp's slow, eventually-consistent API, and
# concurrent worker invocations can ALL pass the comment check before any ack
# comment is visible — exactly how one retried delivery launched 6 Fargate
# agents on 2026-07-14. The authoritative dedup is an atomic conditional
# PutItem claim on (task_id, label) that does not depend on ClickUp at all:
# exactly one worker wins.
# Prod-state contract: DEDUP_TABLE_NAME is unset between the code deploy and
# the terraform apply that creates the table, so the unconfigured state must be
# a quiet no-op — every test outside this section runs in that state.
# ---------------------------------------------------------------------------


def conditional_check_failed() -> ClientError:
    # Built the way boto3 raises it: a botocore ClientError whose Error.Code is
    # "ConditionalCheckFailedException". The handler must match on that code,
    # never on the exception class name.
    return ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "The conditional request failed"}},
        "PutItem",
    )


def test_lock_acquired_writes_claim_item_and_launches(fake_clickup, fake_ecs, fake_dynamodb, ecs_env, dedup_table_env):
    before = int(time.time())
    resp = handler.handler(make_event(tag_updated_body()), None)
    after = int(time.time())

    assert resp["statusCode"] == 200
    assert response_body(resp)["fargate_task_arn"] == TASK_ARN
    assert len(fake_ecs.run_task_calls) == 1

    # Exactly one conditional claim write, keyed on task#label, refusing to
    # overwrite a live (unexpired) claim.
    assert len(fake_dynamodb.put_item_calls) == 1
    call = fake_dynamodb.put_item_calls[0]
    assert call["TableName"] == dedup_table_env
    assert call["ConditionExpression"] == "attribute_not_exists(pk) OR #exp < :now"
    assert call["ExpressionAttributeNames"] == {"#exp": "expires_at"}
    item = call["Item"]
    assert item["pk"] == {"S": "abc123#analyze"}
    assert item["task_id"] == {"S": "abc123"}
    assert item["label"] == {"S": "analyze"}
    # DynamoDB TTL requires epoch SECONDS as a Number attribute.
    expires_at = int(item["expires_at"]["N"])
    assert before + 900 <= expires_at <= after + 900

    # A successful launch KEEPS the claim (DynamoDB TTL expires it); deleting
    # it here would reopen the retry-storm window immediately.
    assert fake_dynamodb.delete_item_calls == []


def test_lock_ttl_configurable_via_env(fake_clickup, fake_ecs, fake_dynamodb, ecs_env, dedup_table_env, monkeypatch):
    monkeypatch.setenv("DEDUP_TTL_SECONDS", "60")
    before = int(time.time())
    resp = handler.handler(make_event(tag_updated_body()), None)
    after = int(time.time())

    assert resp["statusCode"] == 200
    expires_at = int(fake_dynamodb.put_item_calls[0]["Item"]["expires_at"]["N"])
    assert before + 60 <= expires_at <= after + 60


def test_invalid_lock_ttl_falls_back_to_default_quietly(
    fake_clickup, fake_ecs, fake_dynamodb, ecs_env, dedup_table_env, monkeypatch, capsys
):
    # Same contract as DEDUP_COMMENT_WINDOW_SECONDS: a typo'd env var must not
    # crash in-path and must not fire the alarm on every trigger — quiet
    # fallback to the 900s default.
    monkeypatch.setenv("DEDUP_TTL_SECONDS", "not-a-number")
    before = int(time.time())
    resp = handler.handler(make_event(tag_updated_body()), None)
    after = int(time.time())

    assert resp["statusCode"] == 200
    expires_at = int(fake_dynamodb.put_item_calls[0]["Item"]["expires_at"]["N"])
    assert before + 900 <= expires_at <= after + 900
    assert_no_alarm_log_emitted(capsys)


@pytest.mark.parametrize("bad_value", ["nan", "inf", "-inf", "0", "-60", "junk"])
def test_non_finite_or_non_positive_ttl_falls_back_to_default_quietly(monkeypatch, capsys, bad_value):
    # Same guard as DEDUP_COMMENT_WINDOW_SECONDS: float() happily parses
    # 'nan'/'inf'/'-inf', which escape a bare ValueError guard and then crash
    # at int(time.time() + ttl) inside the claim write — in-path, on every
    # trigger. Non-finite or non-positive values must fall back to the
    # default with the existing quiet (non-alarm) line.
    monkeypatch.setenv("DEDUP_TTL_SECONDS", bad_value)

    assert handler.get_dedup_ttl_seconds() == handler.DEFAULT_DEDUP_TTL_SECONDS

    out = assert_no_alarm_log_emitted(capsys)
    assert "Invalid DEDUP_TTL_SECONDS" in out


def test_duplicate_claim_suppresses_launch_quietly_sync_path(
    fake_clickup, fake_ecs, fake_dynamodb, ecs_env, dedup_table_env, capsys
):
    # A lost claim race is the dedup WORKING, not a failure: quiet log, no
    # launch, no ack comment (the winner posts its own), 200 skipped so
    # ClickUp does not re-deliver.
    fake_dynamodb.put_item_exception = conditional_check_failed()

    resp = handler.handler(make_event(tag_updated_body()), None)

    assert resp["statusCode"] == 200
    assert response_body(resp) == {"skipped": "duplicate suppressed"}
    assert fake_ecs.run_task_calls == []
    assert fake_clickup.posted_comments == []
    assert fake_dynamodb.delete_item_calls == []
    out = assert_no_alarm_log_emitted(capsys)
    assert "suppressed by dedup table" in out


def test_duplicate_claim_suppresses_launch_async_worker(
    fake_clickup, fake_ecs, fake_dynamodb, ecs_env, dedup_table_env, capsys
):
    # Same suppression through the async worker path — the concurrent-delivery
    # case the lock exists for (both workers pass the comment check; only one
    # wins the conditional write).
    fake_dynamodb.put_item_exception = conditional_check_failed()
    event = {"gpbot_async": True, "task_id": "abc123", "matched_tag": "gpbot-analyze"}

    resp = handler.handler(event, None)

    assert resp["statusCode"] == 200
    assert response_body(resp) == {"skipped": "duplicate suppressed"}
    assert fake_ecs.run_task_calls == []
    assert fake_clickup.posted_comments == []
    assert_no_alarm_log_emitted(capsys)


@pytest.mark.parametrize(
    "dynamo_error",
    [
        ClientError({"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "slow"}}, "PutItem"),
        RuntimeError("socket timeout talking to dynamodb"),
    ],
    ids=["client-error-other-code", "non-client-error"],
)
def test_dynamo_outage_fails_open_and_alarms(
    fake_clickup, fake_ecs, fake_dynamodb, ecs_env, dedup_table_env, capsys, dynamo_error
):
    # FAIL-OPEN: a broken dedup table must not take the bot down — the launch
    # still happens — but this is real infrastructure breakage an operator
    # must see, so the log line is deliberately alarm-matching.
    fake_dynamodb.put_item_exception = dynamo_error

    resp = handler.handler(make_event(tag_updated_body()), None)

    assert resp["statusCode"] == 200
    assert response_body(resp)["fargate_task_arn"] == TASK_ARN
    assert len(fake_ecs.run_task_calls) == 1
    assert any(text.startswith("[GP-Bot] Processing started") for text in fake_clickup.posted_comment_texts)
    out = assert_alarm_log_emitted(capsys)
    assert "dedup table unavailable" in out


def test_unconfigured_table_skips_dynamo_entirely(
    fake_clickup, fake_ecs, fake_dynamodb, boto3_factory, ecs_env, capsys
):
    # clean_dedup_table_env (autouse) removed DEDUP_TABLE_NAME: the initial
    # prod state between the code deploy and the terraform apply. Everything
    # must work exactly as before — no dynamodb client, no calls, quiet log.
    resp = handler.handler(make_event(tag_updated_body()), None)

    assert resp["statusCode"] == 200
    assert response_body(resp)["fargate_task_arn"] == TASK_ARN
    assert len(fake_ecs.run_task_calls) == 1
    assert "dynamodb" not in boto3_factory.requested_services
    assert fake_dynamodb.put_item_calls == []
    assert fake_dynamodb.delete_item_calls == []
    out = assert_no_alarm_log_emitted(capsys)
    assert "Dedup table not configured" in out


def test_recent_comment_marker_skips_before_any_lock_attempt(
    fake_clickup, fake_ecs, fake_dynamodb, ecs_env, dedup_table_env
):
    # Layer order is a contract: the cheap comment check runs FIRST, and a
    # recent marker must short-circuit without ever writing a claim — a
    # comment-deduped skip that also burned a lock would block a legitimate
    # re-trigger for the whole TTL after the marker expires.
    fake_clickup.comments_response = existing_gpbot_comment_response()

    resp = handler.handler(make_event(tag_updated_body()), None)

    assert resp["statusCode"] == 200
    assert response_body(resp) == {"skipped": "already processed"}
    assert fake_dynamodb.put_item_calls == []


# ---------------------------------------------------------------------------
# 24b. Claim release on launch failure. The documented retry contract is
# "remove and re-add the tag to retry" — failure comments never block it, so
# a failed launch's claim must not either, or the user's retry would be
# silently suppressed until the TTL expires.
# ---------------------------------------------------------------------------


def assert_claim_released(fake_dynamodb: FakeDynamoDBClient, table_name: str, pk: str = "abc123#analyze"):
    assert len(fake_dynamodb.delete_item_calls) == 1
    call = fake_dynamodb.delete_item_calls[0]
    assert call["TableName"] == table_name
    assert call["Key"] == {"pk": {"S": pk}}


def test_launch_exception_releases_claim(fake_clickup, fake_ecs, fake_dynamodb, ecs_env, dedup_table_env, capsys):
    fake_ecs.exception = RuntimeError("ECS exploded")

    resp = handler.handler(make_event(tag_updated_body()), None)

    assert resp["statusCode"] == 500
    assert len(fake_dynamodb.put_item_calls) == 1
    assert_claim_released(fake_dynamodb, dedup_table_env)
    assert_alarm_log_emitted(capsys)


def test_launch_failures_response_releases_claim(fake_clickup, fake_ecs, fake_dynamodb, ecs_env, dedup_table_env):
    fake_ecs.response = {"tasks": [], "failures": [{"reason": "RESOURCE:MEMORY"}]}

    resp = handler.handler(make_event(tag_updated_body()), None)

    assert resp["statusCode"] == 500
    assert_claim_released(fake_dynamodb, dedup_table_env)


def test_missing_ecs_config_releases_claim(fake_clickup, fake_ecs, fake_dynamodb, dedup_table_env):
    # No ecs_env fixture: trigger_fargate_task 500s before run_task. The claim
    # must still be released so the retry after ops fixes the config works.
    resp = handler.handler(make_event(tag_updated_body()), None)

    assert resp["statusCode"] == 500
    assert fake_ecs.run_task_calls == []
    assert_claim_released(fake_dynamodb, dedup_table_env)


def test_release_failure_alarms_but_does_not_change_response(
    fake_clickup, fake_ecs, fake_dynamodb, ecs_env, dedup_table_env, capsys
):
    # A failed DeleteItem leaves a stuck claim that suppresses the user's
    # retry until the TTL expires — alarm-worthy — but it must not change
    # control flow: the launch-failure 500 and its failure comment stand.
    fake_ecs.exception = RuntimeError("ECS exploded")
    fake_dynamodb.delete_item_exception = RuntimeError("dynamodb down")

    resp = handler.handler(make_event(tag_updated_body()), None)

    assert resp["statusCode"] == 500
    assert any("Failed to start processing" in text for text in fake_clickup.posted_comment_texts)
    out = assert_alarm_log_emitted(capsys)
    assert "Failed to release dedup lock" in out
