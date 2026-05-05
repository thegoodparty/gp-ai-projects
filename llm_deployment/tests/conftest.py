from __future__ import annotations

from pathlib import Path

import pytest

from llm_deployment.pod import PodConfig


@pytest.fixture
def tmp_pod_id_file(tmp_path: Path) -> Path:
    return tmp_path / ".pod_id"


@pytest.fixture
def config(tmp_pod_id_file: Path) -> PodConfig:
    return PodConfig(
        runpod_api_key="test-api-key",
        hf_token="test-hf-token",
        model_name="org/TestModel-FP8",
        gpu_type_id="NVIDIA H200 NVL",
        gpu_count=1,
        volume_size_gb=200,
        vllm_port=8000,
        vllm_api_key="test-vllm-key",
        pod_id_file=tmp_pod_id_file,
    )


@pytest.fixture
def config_no_api_key(tmp_pod_id_file: Path) -> PodConfig:
    return PodConfig(
        runpod_api_key="",
        pod_id_file=tmp_pod_id_file,
    )


@pytest.fixture
def config_no_vllm_key(tmp_pod_id_file: Path) -> PodConfig:
    return PodConfig(
        runpod_api_key="test-api-key",
        vllm_api_key="",
        pod_id_file=tmp_pod_id_file,
    )
