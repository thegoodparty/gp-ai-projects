import asyncio
import tempfile
from pathlib import Path

import pytest

from serve.v1_pipeline.pipeline.orchestrator import V1PipelineOrchestrator


@pytest.fixture
def pipeline_with_empty_csv(tmp_path):
    campaign_name = "testcampaign"
    campaign_dir = tmp_path / "input" / campaign_name
    campaign_dir.mkdir(parents=True)

    poll_id = "019c4875-97a2-7889-93d6-13929bc4d6ae"
    csv_file = campaign_dir / f"{poll_id}.csv"
    csv_file.write_bytes(b"")

    output_dir = tmp_path / "output"
    output_dir.mkdir()

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"consolidation:\n"
        f"  input_dir: {tmp_path / 'input'}\n"
        f"  output_dir: {output_dir}\n"
        f"clustering:\n"
        f"  enabled: false\n"
        f"sqs_events:\n"
        f"  enabled: false\n"
    )

    orchestrator = V1PipelineOrchestrator(config_path=str(config_path))

    return orchestrator, campaign_name, poll_id


def test_pipeline_handles_zero_byte_csv(pipeline_with_empty_csv):
    orchestrator, campaign_name, poll_id = pipeline_with_empty_csv

    result = asyncio.run(orchestrator.run_pipeline(campaign_name))

    assert result.errors == [], f"Expected no errors, got: {result.errors}"
    assert result.input_messages == 0
    assert result.output_records == 0

    files = result.consolidation_result.get("files", [])
    poll_ids_in_result = [f["poll_id"] for f in files]
    assert poll_id in poll_ids_in_result, (
        f"Expected poll_id '{poll_id}' in consolidation_result files, got: {files}"
    )


@pytest.fixture
def pipeline_with_mixed_csvs(tmp_path):
    campaign_name = "testcampaign"
    campaign_dir = tmp_path / "input" / campaign_name
    campaign_dir.mkdir(parents=True)

    empty_poll_id = "aaaa-empty-poll"
    empty_csv = campaign_dir / f"{empty_poll_id}.csv"
    empty_csv.write_bytes(b"")

    valid_poll_id = "zzzz-valid-poll"
    valid_csv = campaign_dir / f"{valid_poll_id}.csv"
    valid_csv.write_text(
        "phone_number,message_text\n"
        "+15551234567,I support better roads\n"
    )

    output_dir = tmp_path / "output"
    output_dir.mkdir()

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"consolidation:\n"
        f"  input_dir: {tmp_path / 'input'}\n"
        f"  output_dir: {output_dir}\n"
        f"clustering:\n"
        f"  enabled: false\n"
        f"sqs_events:\n"
        f"  enabled: false\n"
    )

    orchestrator = V1PipelineOrchestrator(config_path=str(config_path))

    return orchestrator, campaign_name, empty_poll_id, valid_poll_id


def test_mixed_empty_and_valid_csvs_uses_correct_poll_id(pipeline_with_mixed_csvs):
    orchestrator, campaign_name, empty_poll_id, valid_poll_id = pipeline_with_mixed_csvs

    result = asyncio.run(orchestrator.run_pipeline(campaign_name))

    assert result.errors == [], f"Expected no errors, got: {result.errors}"
    assert result.input_messages == 1

    files = result.consolidation_result.get("files", [])
    loaded_files = [f for f in files if f.get("status") != "skipped_empty"]
    assert loaded_files[0]["poll_id"] == valid_poll_id, (
        f"First loaded file should be the valid poll, got: {loaded_files}"
    )


@pytest.fixture
def pipeline_with_unparseable_csv(tmp_path):
    campaign_name = "testcampaign"
    campaign_dir = tmp_path / "input" / campaign_name
    campaign_dir.mkdir(parents=True)

    poll_id = "unparseable-poll"
    csv_file = campaign_dir / f"{poll_id}.csv"
    csv_file.write_text("\n\n  \n")

    output_dir = tmp_path / "output"
    output_dir.mkdir()

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"consolidation:\n"
        f"  input_dir: {tmp_path / 'input'}\n"
        f"  output_dir: {output_dir}\n"
        f"clustering:\n"
        f"  enabled: false\n"
        f"sqs_events:\n"
        f"  enabled: false\n"
    )

    orchestrator = V1PipelineOrchestrator(config_path=str(config_path))

    return orchestrator, campaign_name, poll_id


def test_pipeline_handles_whitespace_only_csv(pipeline_with_unparseable_csv):
    orchestrator, campaign_name, poll_id = pipeline_with_unparseable_csv

    result = asyncio.run(orchestrator.run_pipeline(campaign_name))

    assert result.errors == [], f"Expected no errors, got: {result.errors}"
    assert result.input_messages == 0
    assert result.output_records == 0

    files = result.consolidation_result.get("files", [])
    poll_ids_in_result = [f["poll_id"] for f in files]
    assert poll_id in poll_ids_in_result
