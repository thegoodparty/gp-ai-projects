import json
from unittest.mock import patch

import pytest

import pmf_engine.control_plane.scheduler_handler as sched
from pmf_engine.control_plane.job_store import QueuedJob


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
    monkeypatch.setattr(sched, "MAX_CONCURRENT_AGENTS", 3, raising=False)
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
    sched.handler({}, None)
    mock_store.return_value.query_queued.assert_not_called()


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
def test_sweeps_stuck_launching_jobs(mock_launch, mock_sqs, mock_store, mock_count):
    mock_count.return_value = 3  # at cap, no new launches
    store = mock_store.return_value
    store.query_stuck_launching.return_value = [_job("r-stuck", "HIGH")]
    sched.handler({}, None)
    store.mark_failed.assert_called_once_with("r-stuck")
    body = json.loads(mock_sqs.return_value.send_message.call_args.kwargs["MessageBody"])
    assert body["data"]["status"] == "failed"
    assert body["data"]["runId"] == "r-stuck"
