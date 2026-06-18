import json
from unittest.mock import MagicMock, patch

import pytest

import pmf_engine.control_plane.scheduler_handler as sched
from pmf_engine.control_plane.job_store import QueuedJob

# Captured at import, before the autouse fixture stubs the module attribute, so
# the SSM tests below can exercise the real implementation.
_REAL_GET_CAP = sched.get_max_concurrent_agents


def _job(run_id, priority):
    return QueuedJob(
        run_id=run_id,
        experiment_type="smoke_test",
        organization_slug="org-1",
        clerk_user_id="user_1",
        priority=priority,
        params={"state": "WI"},
        routing={
            "model": "sonnet",
            "timeout_seconds": 600,
            "scope": {},
            "manifest_version_id": None,
            "instruction_version_id": None,
            "attachment_version_ids": None,
        },
        prior_artifact_versions=None,
        created_at_ms=1000,
    )


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    # The live cap is read via get_max_concurrent_agents() (SSM each tick); stub
    # it to 3 so the existing slot/cap tests are unaffected by the SSM plumbing.
    monkeypatch.setattr(sched, "get_max_concurrent_agents", lambda: 3)
    monkeypatch.setattr(sched, "RESULTS_QUEUE_URL", "https://sqs/cb.fifo", raising=False)


@patch("pmf_engine.control_plane.scheduler_handler.count_running_tasks")
@patch("pmf_engine.control_plane.scheduler_handler.get_job_store")
@patch("pmf_engine.control_plane.scheduler_handler.get_sqs_client")
@patch("pmf_engine.control_plane.scheduler_handler.launch_run")
def test_launches_up_to_free_slots(mock_launch, mock_sqs, mock_store, mock_count):
    mock_count.return_value = 1  # cap 3 -> 2 slots
    store = mock_store.return_value
    store.query_queued.return_value = [_job("r-high", "HIGH"), _job("r-def", "DEFAULT")]
    store.query_stuck_launching.return_value = []
    mock_launch.return_value = {"status": "launched", "task_arn": "arn:task/x"}

    sched.handler({}, None)

    store.query_queued.assert_called_once_with(limit=2)
    assert mock_launch.call_count == 2
    assert store.claim.call_count == 2
    assert store.mark_dispatched.call_count == 2
    assert mock_sqs.return_value.send_message.call_count == 2
    body = json.loads(mock_sqs.return_value.send_message.call_args_list[0].kwargs["MessageBody"])
    assert body["data"]["status"] == "started"


@patch("pmf_engine.control_plane.scheduler_handler.count_running_tasks")
@patch("pmf_engine.control_plane.scheduler_handler.get_job_store")
def test_no_launch_when_at_cap(mock_store, mock_count):
    mock_count.return_value = 3  # cap 3 -> 0 slots
    store = mock_store.return_value
    # Stub the sweep query so _sweep_stuck_launching runs cleanly — without this
    # it iterates a bare MagicMock and TypeErrors, which the handler's try/except
    # swallows, letting the test pass without actually exercising the cap path.
    store.query_stuck_launching.return_value = []
    sched.handler({}, None)
    store.query_queued.assert_not_called()


@patch("pmf_engine.control_plane.scheduler_handler.count_running_tasks")
@patch("pmf_engine.control_plane.scheduler_handler.get_job_store")
@patch("pmf_engine.control_plane.scheduler_handler.get_sqs_client")
@patch("pmf_engine.control_plane.scheduler_handler.launch_run")
def test_skips_jobs_lost_to_claim_race(mock_launch, mock_sqs, mock_store, mock_count):
    from pmf_engine.control_plane.job_store import JobClaimConflict

    mock_count.return_value = 0
    store = mock_store.return_value
    store.query_queued.return_value = [_job("r1", "HIGH")]
    store.query_stuck_launching.return_value = []
    store.claim.side_effect = JobClaimConflict("r1")
    sched.handler({}, None)
    mock_launch.assert_not_called()


@patch("pmf_engine.control_plane.scheduler_handler.count_running_tasks")
@patch("pmf_engine.control_plane.scheduler_handler.get_job_store")
@patch("pmf_engine.control_plane.scheduler_handler.get_sqs_client")
@patch("pmf_engine.control_plane.scheduler_handler.launch_run")
def test_failed_launch_sends_failed_callback_and_marks_failed(mock_launch, mock_sqs, mock_store, mock_count):
    mock_count.return_value = 0
    store = mock_store.return_value
    store.query_queued.return_value = [_job("r1", "HIGH")]
    store.query_stuck_launching.return_value = []
    mock_launch.return_value = {"status": "failed", "error": "Broker rejected the request"}
    sched.handler({}, None)
    store.mark_failed.assert_called_once_with("r1")
    body = json.loads(mock_sqs.return_value.send_message.call_args.kwargs["MessageBody"])
    assert body["data"]["status"] == "failed"
    assert body["data"]["error"] == "Broker rejected the request"


@patch("pmf_engine.control_plane.scheduler_handler.count_running_tasks")
@patch("pmf_engine.control_plane.scheduler_handler.get_job_store")
@patch("pmf_engine.control_plane.scheduler_handler.get_sqs_client")
@patch("pmf_engine.control_plane.scheduler_handler.launch_run")
def test_transient_launch_leaves_job_launching(mock_launch, mock_sqs, mock_store, mock_count):
    mock_count.return_value = 0
    store = mock_store.return_value
    store.query_queued.return_value = [_job("r1", "HIGH")]
    store.query_stuck_launching.return_value = []
    mock_launch.side_effect = RuntimeError("ECS transient")
    sched.handler({}, None)
    store.mark_dispatched.assert_not_called()
    store.mark_failed.assert_not_called()


@patch("pmf_engine.control_plane.scheduler_handler.count_running_tasks")
@patch("pmf_engine.control_plane.scheduler_handler.get_job_store")
@patch("pmf_engine.control_plane.scheduler_handler.get_sqs_client")
@patch("pmf_engine.control_plane.scheduler_handler.launch_run")
def test_started_callback_send_failure_still_dispatches(mock_launch, mock_sqs, mock_store, mock_count):
    # A successful launch means a real task is running, so the job MUST be marked
    # dispatched even if the `started` callback send fails — otherwise the sweep
    # would later FAIL a live task. The job must NOT be failed or left LAUNCHING;
    # gp-api reconciles via the terminal callback / backstop. Handler returns
    # the launch as counted, no exception escapes.
    mock_count.return_value = 0
    store = mock_store.return_value
    store.query_queued.return_value = [_job("r1", "HIGH")]
    store.query_stuck_launching.return_value = []
    mock_launch.return_value = {"status": "launched", "task_arn": "arn:task/x"}
    mock_sqs.return_value.send_message.side_effect = RuntimeError("sqs down")

    result = sched.handler({}, None)

    store.mark_dispatched.assert_called_once_with("r1")
    store.mark_failed.assert_not_called()
    assert result == {"launched": 1}


@patch("pmf_engine.control_plane.scheduler_handler.count_running_tasks")
@patch("pmf_engine.control_plane.scheduler_handler.get_job_store")
@patch("pmf_engine.control_plane.scheduler_handler.get_sqs_client")
@patch("pmf_engine.control_plane.scheduler_handler.launch_run")
def test_one_job_error_does_not_abort_batch(mock_launch, mock_sqs, mock_store, mock_count):
    # An unexpected error on one job must not abort the loop — the next job
    # still launches and the handler returns normally.
    mock_count.return_value = 0
    store = mock_store.return_value
    store.query_queued.return_value = [_job("r-bad", "HIGH"), _job("r-good", "DEFAULT")]
    store.query_stuck_launching.return_value = []
    store.claim.side_effect = [RuntimeError("ddb blip"), None]
    mock_launch.return_value = {"status": "launched", "task_arn": "arn:task/x"}

    result = sched.handler({}, None)

    assert result == {"launched": 1}
    store.mark_dispatched.assert_called_once_with("r-good")


@patch("pmf_engine.control_plane.scheduler_handler._has_live_task")
@patch("pmf_engine.control_plane.scheduler_handler.count_running_tasks")
@patch("pmf_engine.control_plane.scheduler_handler.get_job_store")
@patch("pmf_engine.control_plane.scheduler_handler.get_sqs_client")
@patch("pmf_engine.control_plane.scheduler_handler.launch_run")
def test_sweeps_stuck_launching_jobs(mock_launch, mock_sqs, mock_store, mock_count, mock_live):
    mock_count.return_value = 3  # at cap, no new launches
    mock_live.return_value = False  # no live task → genuinely stuck, fail it
    store = mock_store.return_value
    store.query_stuck_launching.return_value = [_job("r-stuck", "HIGH")]
    sched.handler({}, None)
    store.mark_failed.assert_called_once_with("r-stuck")
    body = json.loads(mock_sqs.return_value.send_message.call_args.kwargs["MessageBody"])
    assert body["data"]["status"] == "failed"
    assert body["data"]["runId"] == "r-stuck"


@patch("pmf_engine.control_plane.scheduler_handler._has_live_task")
@patch("pmf_engine.control_plane.scheduler_handler.count_running_tasks")
@patch("pmf_engine.control_plane.scheduler_handler.get_job_store")
@patch("pmf_engine.control_plane.scheduler_handler.get_sqs_client")
@patch("pmf_engine.control_plane.scheduler_handler.launch_run")
def test_sweep_reconciles_stuck_job_with_live_task(mock_launch, mock_sqs, mock_store, mock_count, mock_live):
    # A job stuck LAUNCHING but with a live Fargate task (mark_dispatched threw
    # after run_task succeeded) must be reconciled to DISPATCHED, NOT failed —
    # failing it would kill a running agent and drop its result.
    mock_count.return_value = 3
    mock_live.return_value = True  # a task tagged with this run_id is alive
    store = mock_store.return_value
    store.query_stuck_launching.return_value = [_job("r-live", "HIGH")]
    sched.handler({}, None)
    store.mark_dispatched.assert_called_once_with("r-live")
    store.mark_failed.assert_not_called()
    mock_sqs.return_value.send_message.assert_not_called()


@patch("pmf_engine.control_plane.scheduler_handler.emit_dispatch_metric")
@patch("pmf_engine.control_plane.scheduler_handler.count_running_tasks")
@patch("pmf_engine.control_plane.scheduler_handler.get_job_store")
@patch("pmf_engine.control_plane.scheduler_handler.get_sqs_client")
@patch("pmf_engine.control_plane.scheduler_handler.launch_run")
def test_failed_launch_callback_send_failure_is_logged_and_metriced(
    mock_launch, mock_sqs, mock_store, mock_count, mock_metric
):
    # A failed launch whose `failed` callback send raises must NOT mark_failed —
    # the job is left in LAUNCHING so the stuck-LAUNCHING sweep retries the
    # callback later (a FAILED row drops out of the sweep and can never be
    # re-notified). It emits the orphan metric and returns normally.
    mock_count.return_value = 0
    store = mock_store.return_value
    store.query_queued.return_value = [_job("r1", "HIGH")]
    store.query_stuck_launching.return_value = []
    mock_launch.return_value = {"status": "failed", "error": "Broker rejected the request"}
    mock_sqs.return_value.send_message.side_effect = RuntimeError("sqs down")

    result = sched.handler({}, None)

    store.mark_failed.assert_not_called()
    assert result == {"launched": 0}
    assert "SchedulerFailedCallbackFailed" in [c.args[0] for c in mock_metric.call_args_list]


@patch("pmf_engine.control_plane.scheduler_handler._has_live_task")
@patch("pmf_engine.control_plane.scheduler_handler.emit_dispatch_metric")
@patch("pmf_engine.control_plane.scheduler_handler.count_running_tasks")
@patch("pmf_engine.control_plane.scheduler_handler.get_job_store")
@patch("pmf_engine.control_plane.scheduler_handler.get_sqs_client")
@patch("pmf_engine.control_plane.scheduler_handler.launch_run")
def test_sweep_callback_send_failure_is_logged_and_metriced(
    mock_launch, mock_sqs, mock_store, mock_count, mock_metric, mock_live
):
    # The sweep's `failed` callback send raising must not abort the sweep or the
    # handler; it logs + emits the orphan metric and leaves the job in LAUNCHING
    # (does NOT mark_failed) so the next sweep retries the callback.
    mock_count.return_value = 3  # at cap, no new launches
    mock_live.return_value = False  # no live task → genuinely stuck
    store = mock_store.return_value
    store.query_stuck_launching.return_value = [_job("r-stuck", "HIGH")]
    mock_sqs.return_value.send_message.side_effect = RuntimeError("sqs down")

    result = sched.handler({}, None)

    store.mark_failed.assert_not_called()
    assert result == {"launched": 0}
    assert "SchedulerSweepCallbackFailed" in [c.args[0] for c in mock_metric.call_args_list]


@patch("pmf_engine.control_plane.scheduler_handler.count_running_tasks")
@patch("pmf_engine.control_plane.scheduler_handler.get_job_store")
@patch("pmf_engine.control_plane.scheduler_handler.get_sqs_client")
@patch("pmf_engine.control_plane.scheduler_handler.launch_run")
def test_query_queued_failure_does_not_propagate(mock_launch, mock_sqs, mock_store, mock_count):
    # A DynamoDB throttle on query_queued must not raise out of handler (which
    # would stall the stream shard); it skips the tick and returns normally.
    mock_count.return_value = 0
    store = mock_store.return_value
    store.query_stuck_launching.return_value = []
    store.query_queued.side_effect = RuntimeError("ProvisionedThroughputExceeded")

    result = sched.handler({}, None)

    assert result == {"launched": 0}
    mock_launch.assert_not_called()


class TestGetMaxConcurrentAgents:
    def test_reads_live_value_from_ssm(self, monkeypatch):
        monkeypatch.setattr(
            sched, "MAX_CONCURRENT_AGENTS_PARAM", "/pmf-engine/dev/max-concurrent-agents", raising=False
        )
        fake = MagicMock()
        fake.get_parameter.return_value = {"Parameter": {"Value": "150"}}
        monkeypatch.setattr(sched, "get_ssm_client", lambda: fake)
        assert _REAL_GET_CAP() == 150
        fake.get_parameter.assert_called_once_with(Name="/pmf-engine/dev/max-concurrent-agents")

    def test_falls_back_to_env_when_ssm_read_fails(self, monkeypatch):
        monkeypatch.setattr(
            sched, "MAX_CONCURRENT_AGENTS_PARAM", "/pmf-engine/dev/max-concurrent-agents", raising=False
        )
        monkeypatch.setattr(sched, "MAX_CONCURRENT_AGENTS_ENV", 100, raising=False)
        fake = MagicMock()
        fake.get_parameter.side_effect = RuntimeError("ssm unavailable")
        monkeypatch.setattr(sched, "get_ssm_client", lambda: fake)
        assert _REAL_GET_CAP() == 100

    def test_uses_env_when_no_param_configured(self, monkeypatch):
        monkeypatch.setattr(sched, "MAX_CONCURRENT_AGENTS_PARAM", "", raising=False)
        monkeypatch.setattr(sched, "MAX_CONCURRENT_AGENTS_ENV", 42, raising=False)
        # get_ssm_client must not even be called when no param is configured.
        monkeypatch.setattr(
            sched, "get_ssm_client", lambda: (_ for _ in ()).throw(AssertionError("SSM should not be queried"))
        )
        assert _REAL_GET_CAP() == 42
