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
