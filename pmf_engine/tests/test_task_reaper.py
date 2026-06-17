import json
from unittest.mock import patch

import pytest

import pmf_engine.control_plane.task_reaper as reaper


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(reaper, "RESULTS_QUEUE_URL", "https://sqs.example.com/results.fifo", raising=False)
    monkeypatch.setattr(reaper, "CONTAINER_NAME", "pmf-engine", raising=False)


def _event(*, last_status="STOPPED", started_by="run-001", containers=None, stop_code="EssentialContainerExited"):
    return {
        "detail": {
            "lastStatus": last_status,
            "startedBy": started_by,
            "stopCode": stop_code,
            "stoppedReason": "Essential container in task exited",
            "containers": containers if containers is not None else [{"name": "pmf-engine", "exitCode": 137}],
        }
    }


@patch("pmf_engine.control_plane.task_reaper.get_sqs_client")
def test_nonzero_exit_sends_failed_callback(mock_sqs):
    reaper.handler(_event(containers=[{"name": "pmf-engine", "exitCode": 137}]), None)
    mock_sqs.return_value.send_message.assert_called_once()
    kwargs = mock_sqs.return_value.send_message.call_args.kwargs
    body = json.loads(kwargs["MessageBody"])
    assert body["data"]["runId"] == "run-001"
    assert body["data"]["status"] == "failed"
    assert body["data"]["reasonCode"] == "TaskStopped"
    assert kwargs["MessageGroupId"] == "agentExperiments"
    assert kwargs["MessageDeduplicationId"] == "run-001-task-stopped"


@patch("pmf_engine.control_plane.task_reaper.get_sqs_client")
def test_clean_exit_does_nothing(mock_sqs):
    # exitCode 0 — the runner reported its own result; the reaper stays out.
    reaper.handler(_event(containers=[{"name": "pmf-engine", "exitCode": 0}]), None)
    mock_sqs.return_value.send_message.assert_not_called()


@patch("pmf_engine.control_plane.task_reaper.get_sqs_client")
def test_failed_to_start_no_exit_code_sends_failed(mock_sqs):
    # TaskFailedToStart — no container ran, so no exitCode. Treated as abnormal.
    reaper.handler(_event(containers=[], stop_code="TaskFailedToStart"), None)
    mock_sqs.return_value.send_message.assert_called_once()
    body = json.loads(mock_sqs.return_value.send_message.call_args.kwargs["MessageBody"])
    assert body["data"]["status"] == "failed"


@patch("pmf_engine.control_plane.task_reaper.get_sqs_client")
def test_ignores_non_stopped_events(mock_sqs):
    reaper.handler(_event(last_status="RUNNING"), None)
    mock_sqs.return_value.send_message.assert_not_called()


@patch("pmf_engine.control_plane.task_reaper.get_sqs_client")
def test_ignores_tasks_without_run_id(mock_sqs):
    # No startedBy → not a scheduler-launched agent task.
    reaper.handler(_event(started_by=None), None)
    mock_sqs.return_value.send_message.assert_not_called()
