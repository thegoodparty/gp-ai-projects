from __future__ import annotations

import httpx
import pytest

from llm_deployment.benchmark import (
    PROMPTS,
    BenchmarkConfig,
    run_concurrent,
    run_sequential,
    single_request,
)


@pytest.fixture
def bench_config() -> BenchmarkConfig:
    return BenchmarkConfig(
        pod_id="test-pod-123",
        vllm_port=8000,
        vllm_api_key="test-key",
        model_name="org/TestModel",
    )


@pytest.fixture
def bench_config_no_key() -> BenchmarkConfig:
    return BenchmarkConfig(
        pod_id="test-pod-123",
        vllm_api_key="",
    )


class TestBenchmarkConfig:
    def test_base_url(self, bench_config):
        assert bench_config.base_url == "https://test-pod-123-8000.proxy.runpod.net/v1"

    def test_headers_with_key(self, bench_config):
        h = bench_config.headers
        assert h["Authorization"] == "Bearer test-key"
        assert h["Content-Type"] == "application/json"

    def test_headers_without_key(self, bench_config_no_key):
        h = bench_config_no_key.headers
        assert "Authorization" not in h
        assert h["Content-Type"] == "application/json"

    def test_from_env_missing_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("llm_deployment.benchmark.SCRIPT_DIR", tmp_path)
        with pytest.raises(FileNotFoundError, match="No .pod_id file found"):
            BenchmarkConfig.from_env(tmp_path / ".env")

    def test_from_env_empty_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("llm_deployment.benchmark.SCRIPT_DIR", tmp_path)
        (tmp_path / ".pod_id").write_text("  \n")
        with pytest.raises(FileNotFoundError, match="empty"):
            BenchmarkConfig.from_env(tmp_path / ".env")


class TestSingleRequestNonStream:
    async def test_success_with_usage(self, bench_config):
        async def handler(request):
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "4"}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await single_request(client, PROMPTS[0], bench_config, stream=False)

        assert result["name"] == "short"
        assert result["prompt_tokens"] == 10
        assert result["completion_tokens"] == 5
        assert result["total_time"] > 0
        assert result["tok_per_sec"] > 0

    async def test_missing_usage_defaults_to_zero(self, bench_config):
        async def handler(request):
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "4"}}],
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await single_request(client, PROMPTS[0], bench_config, stream=False)

        assert result["completion_tokens"] == 0
        assert result["prompt_tokens"] == 0


class TestSingleRequestStream:
    async def test_counts_content_tokens(self, bench_config):
        lines = [
            'data: {"choices": [{"delta": {"content": "Hello"}}]}',
            'data: {"choices": [{"delta": {"content": " world"}}]}',
            "data: [DONE]",
        ]
        body = "\n".join(lines)

        async def handler(request):
            return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await single_request(client, PROMPTS[0], bench_config, stream=True)

        assert result["tokens"] == 2
        assert result["ttfb"] > 0

    async def test_counts_reasoning_content(self, bench_config):
        lines = [
            'data: {"choices": [{"delta": {"reasoning_content": "thinking..."}}]}',
            'data: {"choices": [{"delta": {"content": "answer"}}]}',
            "data: [DONE]",
        ]
        body = "\n".join(lines)

        async def handler(request):
            return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await single_request(client, PROMPTS[0], bench_config, stream=True)

        assert result["tokens"] == 2

    async def test_empty_stream(self, bench_config):
        async def handler(request):
            return httpx.Response(200, content=b"data: [DONE]\n", headers={"content-type": "text/event-stream"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await single_request(client, PROMPTS[0], bench_config, stream=True)

        assert result["tokens"] == 0

    async def test_malformed_json_skipped(self, bench_config):
        lines = [
            'data: {"choices": [{"delta": {"content": "ok"}}]}',
            "data: {malformed json",
            "data: [DONE]",
        ]
        body = "\n".join(lines)

        async def handler(request):
            return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await single_request(client, PROMPTS[0], bench_config, stream=True)

        assert result["tokens"] == 1


class TestRunSequential:
    async def test_processes_all_prompts(self, bench_config):
        async def handler(request):
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "response"}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 20},
                },
            )

        transport = httpx.MockTransport(handler)
        original_init = httpx.AsyncClient.__init__

        def patched_init(self_client, **kwargs):
            original_init(self_client, transport=transport)

        import unittest.mock

        with unittest.mock.patch.object(httpx.AsyncClient, "__init__", patched_init):
            results = await run_sequential(bench_config, stream=False)

        assert len(results) == len(PROMPTS)
        assert all(r["completion_tokens"] == 20 for r in results)


class TestRunConcurrent:
    async def test_runs_n_requests(self, bench_config):
        async def handler(request):
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "response"}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 20},
                },
            )

        transport = httpx.MockTransport(handler)
        original_init = httpx.AsyncClient.__init__

        def patched_init(self_client, **kwargs):
            original_init(self_client, transport=transport)

        import unittest.mock

        with unittest.mock.patch.object(httpx.AsyncClient, "__init__", patched_init):
            results = await run_concurrent(bench_config, concurrency=4, prompt_idx=0)

        assert len(results) == 4
        assert all(r["completion_tokens"] == 20 for r in results)
