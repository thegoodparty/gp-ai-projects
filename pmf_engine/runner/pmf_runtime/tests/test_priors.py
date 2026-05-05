import json
import httpx
import pytest

from pmf_engine.runner.pmf_runtime.config import init_config
from pmf_engine.runner.pmf_runtime.priors import read


def _inject_client(handler):
    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, base_url="http://broker")
    cfg = init_config("http://broker", "tok")
    cfg._client = client
    return cfg


class TestRead:
    def setup_method(self):
        import pmf_engine.runner.pmf_runtime.config as config_mod
        config_mod._config = None

    def test_read_success(self):
        def handler(request):
            body = json.loads(request.content)
            assert body["experiment_id"] == "exp-001"
            assert body["latest"] is True
            return httpx.Response(200, json={"content": "raw", "artifact": {"key": "value"}})

        _inject_client(handler)
        result = read("exp-001")
        assert result == {"key": "value"}

    def test_read_400_raises_value_error(self):
        def handler(request):
            return httpx.Response(400, json={"detail": "flagged"})

        _inject_client(handler)
        with pytest.raises(ValueError, match="Prior artifact flagged"):
            read("exp-bad")

    def test_read_404_raises_file_not_found(self):
        def handler(request):
            return httpx.Response(404)

        _inject_client(handler)
        with pytest.raises(FileNotFoundError, match="No prior artifact for exp-missing"):
            read("exp-missing")

    def test_read_latest_false(self):
        captured = {}

        def handler(request):
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={"artifact": {"data": "ok"}})

        _inject_client(handler)
        result = read("exp-001", latest=False)
        assert captured["body"]["latest"] is False
        assert result == {"data": "ok"}
