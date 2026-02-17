from __future__ import annotations

import httpx
import pytest

from llm_deployment.pod import (
    PodConfig,
    PodError,
    api,
    base_url,
    cmd_destroy,
    cmd_launch,
    cmd_start,
    cmd_status,
    cmd_stop,
    cmd_test,
    cmd_url,
    cmd_wait,
    get_pod_id,
    main,
    save_pod_id,
    vllm_headers,
)


def _mock_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


class TestPodConfig:
    def test_pod_name_derived_from_model(self):
        cfg = PodConfig(model_name="Qwen/Qwen3-Coder-Next-FP8")
        assert cfg.pod_name == "vllm-qwen3-coder-next-fp8"

    def test_pod_name_no_org_prefix(self):
        cfg = PodConfig(model_name="MyModel")
        assert cfg.pod_name == "vllm-mymodel"

    def test_graphql_url_includes_api_key(self):
        cfg = PodConfig(runpod_api_key="abc123")
        assert cfg.graphql_url == "https://api.runpod.io/graphql?api_key=abc123"

    def test_from_env_invalid_int(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("")
        monkeypatch.setenv("GPU_COUNT", "not-a-number")
        with pytest.raises(PodError, match="Invalid integer for GPU_COUNT"):
            PodConfig.from_env(env_file)


class TestApi:
    def test_success(self, config):
        payload = {"data": {"result": "ok"}}

        def handler(request):
            return httpx.Response(200, json=payload)

        result = api("{ query }", config, client=_mock_client(handler))
        assert result == payload

    def test_http_error_raises(self, config):
        def handler(request):
            return httpx.Response(500, text="Internal Server Error")

        with pytest.raises(PodError, match="API error \\(500\\)"):
            api("{ query }", config, client=_mock_client(handler))

    def test_graphql_error_raises(self, config):
        def handler(request):
            return httpx.Response(200, json={"errors": [{"message": "bad field"}]})

        with pytest.raises(PodError, match="GraphQL errors"):
            api("{ query }", config, client=_mock_client(handler))

    def test_non_json_response_raises(self, config):
        def handler(request):
            return httpx.Response(200, text="<html>Error</html>", headers={"content-type": "text/html"})

        with pytest.raises(PodError, match="Non-JSON response"):
            api("{ query }", config, client=_mock_client(handler))


class TestGetSavePodId:
    def test_no_file_returns_empty(self, config):
        assert get_pod_id(config) == ""

    def test_reads_file(self, config):
        config.pod_id_file.write_text("abc123")
        assert get_pod_id(config) == "abc123"

    def test_strips_whitespace(self, config):
        config.pod_id_file.write_text("  abc123  \n")
        assert get_pod_id(config) == "abc123"

    def test_whitespace_only_returns_empty(self, config):
        config.pod_id_file.write_text("  \n  ")
        assert get_pod_id(config) == ""

    def test_save_overwrites(self, config):
        save_pod_id("first", config)
        save_pod_id("second", config)
        assert get_pod_id(config) == "second"


class TestBaseUrlAndHeaders:
    def test_base_url_format(self, config):
        assert base_url("pod123", config) == "https://pod123-8000.proxy.runpod.net/v1"

    def test_base_url_custom_port(self, config):
        config.vllm_port = 9000
        assert base_url("pod123", config) == "https://pod123-9000.proxy.runpod.net/v1"

    def test_vllm_headers_with_key(self, config):
        assert vllm_headers(config) == {"Authorization": "Bearer test-vllm-key"}

    def test_vllm_headers_without_key(self, config_no_vllm_key):
        assert vllm_headers(config_no_vllm_key) == {}


class TestCmdLaunch:
    def test_missing_api_key_raises(self, config_no_api_key):
        with pytest.raises(PodError, match="RUNPOD_API_KEY not set"):
            cmd_launch(config_no_api_key)

    def test_existing_pod_raises(self, config):
        save_pod_id("existing-pod", config)
        with pytest.raises(PodError, match="already tracked"):
            cmd_launch(config)

    def test_success_saves_pod_id(self, config):
        def handler(request):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "podFindAndDeployOnDemand": {
                            "id": "new-pod-123",
                            "name": "test",
                            "desiredStatus": "RUNNING",
                            "imageName": "img",
                            "machine": {"gpuDisplayName": "H200"},
                        }
                    }
                },
            )

        cmd_launch(config, client=_mock_client(handler))
        assert get_pod_id(config) == "new-pod-123"

    def test_api_failure_raises(self, config):
        def handler(request):
            return httpx.Response(200, json={"data": {"podFindAndDeployOnDemand": None}})

        with pytest.raises(PodError, match="Failed to create pod"):
            cmd_launch(config, client=_mock_client(handler))


class TestCmdWait:
    def test_no_pod_raises(self, config):
        with pytest.raises(PodError, match="No pod tracked"):
            cmd_wait(config)

    def test_immediate_success(self, config):
        save_pod_id("pod123", config)

        def handler(request):
            return httpx.Response(200, json={"data": [{"id": "model-1"}]})

        sleeps = []
        cmd_wait(config, client=_mock_client(handler), sleep_fn=sleeps.append, max_attempts=3)
        assert len(sleeps) == 0

    def test_retries_then_success(self, config):
        save_pod_id("pod123", config)
        call_count = 0

        def handler(request):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                return httpx.Response(503, text="not ready")
            return httpx.Response(200, json={"data": [{"id": "model-1"}]})

        sleeps = []
        cmd_wait(config, client=_mock_client(handler), sleep_fn=sleeps.append, max_attempts=5)
        assert len(sleeps) == 2
        assert call_count == 3

    def test_non_json_200_retries(self, config):
        save_pod_id("pod123", config)
        call_count = 0

        def handler(request):
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                return httpx.Response(200, text="<html>loading</html>", headers={"content-type": "text/html"})
            return httpx.Response(200, json={"data": [{"id": "model-1"}]})

        sleeps = []
        cmd_wait(config, client=_mock_client(handler), sleep_fn=sleeps.append, max_attempts=3)
        assert call_count == 2
        assert len(sleeps) == 1

    def test_timeout_raises(self, config):
        save_pod_id("pod123", config)

        def handler(request):
            return httpx.Response(503, text="not ready")

        with pytest.raises(PodError, match="Timed out"):
            cmd_wait(config, client=_mock_client(handler), sleep_fn=lambda _: None, max_attempts=2)


class TestCmdStatus:
    def test_no_api_key_raises(self, config_no_api_key):
        with pytest.raises(PodError, match="RUNPOD_API_KEY not set"):
            cmd_status(config_no_api_key)

    def test_no_pods(self, config, capsys):
        def handler(request):
            return httpx.Response(200, json={"data": {"myself": {"pods": []}}})

        cmd_status(config, client=_mock_client(handler))
        assert "No pods found" in capsys.readouterr().out

    def test_stale_pod_id_cleaned_up_when_no_pods(self, config, capsys):
        save_pod_id("dead-pod", config)

        def handler(request):
            return httpx.Response(200, json={"data": {"myself": {"pods": []}}})

        cmd_status(config, client=_mock_client(handler))
        out = capsys.readouterr().out
        assert "Removed stale .pod_id" in out
        assert get_pod_id(config) == ""

    def test_stale_pod_id_cleaned_up_when_not_in_list(self, config, capsys):
        save_pod_id("dead-pod", config)

        def handler(request):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "myself": {
                            "pods": [
                                {
                                    "id": "other-pod",
                                    "name": "alive",
                                    "desiredStatus": "RUNNING",
                                    "runtime": {"uptimeInSeconds": 100},
                                    "machine": {"gpuDisplayName": "H200"},
                                },
                            ]
                        }
                    }
                },
            )

        cmd_status(config, client=_mock_client(handler))
        out = capsys.readouterr().out
        assert "Removed stale .pod_id" in out
        assert get_pod_id(config) == ""

    def test_tracked_pod_in_list_not_removed(self, config, capsys):
        save_pod_id("p1", config)

        def handler(request):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "myself": {
                            "pods": [
                                {
                                    "id": "p1",
                                    "name": "my-pod",
                                    "desiredStatus": "RUNNING",
                                    "runtime": {"uptimeInSeconds": 100},
                                    "machine": {"gpuDisplayName": "H200"},
                                },
                            ]
                        }
                    }
                },
            )

        cmd_status(config, client=_mock_client(handler))
        assert "stale" not in capsys.readouterr().out
        assert get_pod_id(config) == "p1"

    def test_multiple_pods(self, config, capsys):
        def handler(request):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "myself": {
                            "pods": [
                                {
                                    "id": "p1",
                                    "name": "pod-one",
                                    "desiredStatus": "RUNNING",
                                    "runtime": {"uptimeInSeconds": 7200},
                                    "machine": {"gpuDisplayName": "H200"},
                                },
                                {
                                    "id": "p2",
                                    "name": "pod-two",
                                    "desiredStatus": "EXITED",
                                    "runtime": None,
                                    "machine": {"gpuDisplayName": "A100"},
                                },
                            ]
                        }
                    }
                },
            )

        cmd_status(config, client=_mock_client(handler))
        out = capsys.readouterr().out
        assert "p1" in out
        assert "p2" in out
        assert "2h 0m" in out
        assert "N/A" in out


class TestCmdStopStart:
    def test_stop_no_pod_raises(self, config):
        with pytest.raises(PodError, match="No pod tracked"):
            cmd_stop(config)

    def test_stop_success(self, config, capsys):
        save_pod_id("pod123", config)

        def handler(request):
            return httpx.Response(200, json={"data": {"podStop": {"id": "pod123", "desiredStatus": "EXITED"}}})

        cmd_stop(config, client=_mock_client(handler))
        assert "Stopped pod pod123" in capsys.readouterr().out

    def test_start_no_pod_raises(self, config):
        with pytest.raises(PodError, match="No pod tracked"):
            cmd_start(config)

    def test_start_success(self, config, capsys):
        save_pod_id("pod123", config)

        def handler(request):
            return httpx.Response(200, json={"data": {"podResume": {"id": "pod123", "desiredStatus": "RUNNING"}}})

        cmd_start(config, client=_mock_client(handler))
        assert "Started pod pod123" in capsys.readouterr().out


class TestCmdDestroy:
    def test_no_pod_raises(self, config):
        with pytest.raises(PodError, match="No pod tracked"):
            cmd_destroy(config)

    def test_user_confirms(self, config, capsys):
        save_pod_id("pod123", config)

        def handler(request):
            return httpx.Response(200, json={"data": {"podTerminate": "pod123"}})

        cmd_destroy(config, client=_mock_client(handler), confirm_fn=lambda _: "y")
        assert "destroyed" in capsys.readouterr().out
        assert not config.pod_id_file.exists()

    def test_user_cancels(self, config, capsys):
        save_pod_id("pod123", config)
        cmd_destroy(config, confirm_fn=lambda _: "n")
        assert "Aborted" in capsys.readouterr().out
        assert get_pod_id(config) == "pod123"


class TestCmdUrlAndTest:
    def test_url_no_pod_raises(self, config):
        with pytest.raises(PodError, match="No pod tracked"):
            cmd_url(config)

    def test_url_prints(self, config, capsys):
        save_pod_id("pod123", config)
        cmd_url(config)
        assert "pod123-8000" in capsys.readouterr().out

    def test_test_no_pod_raises(self, config):
        with pytest.raises(PodError, match="No pod tracked"):
            cmd_test(config)

    def test_test_success(self, config, capsys):
        save_pod_id("pod123", config)

        def handler(request):
            if "/models" in str(request.url):
                return httpx.Response(200, json={"data": [{"id": "model-1"}]})
            return httpx.Response(200, json={"choices": [{"message": {"content": "hello"}}]})

        cmd_test(config, client=_mock_client(handler))
        out = capsys.readouterr().out
        assert "/v1/models" in out
        assert "/v1/chat/completions" in out


class TestMain:
    def test_no_command_exits_zero(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["pod.py"])
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 0

    def test_pod_error_exits_one(self, monkeypatch, config_no_api_key, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("RUNPOD_API_KEY=\n")
        monkeypatch.setattr("sys.argv", ["pod.py", "status"])
        monkeypatch.setattr("llm_deployment.pod.SCRIPT_DIR", tmp_path)
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1
