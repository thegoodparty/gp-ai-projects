import json
from unittest.mock import patch, MagicMock

import pytest

from pmf_engine.control_plane.broker_client import BrokerClient, BrokerError


class TestDeleteRunToken:
    """Covers CRITICAL #1 companion: after ecs.run_task fails following a
    successful mint, dispatch_handler must call DELETE to free the run-lock
    so the same run_id can be re-dispatched without falsely 409-ing."""

    def test_posts_to_internal_delete_run_token(self):
        mock_response = MagicMock()
        mock_response.status_code = 204
        mock_response.raise_for_status = MagicMock()

        with patch("pmf_engine.control_plane.broker_client.httpx.post", return_value=mock_response) as mock_post:
            client = BrokerClient("https://broker.example.com", "svc-token-xyz")
            client.delete_run_token(broker_token="tok-abc", run_id="run-001")

        url = mock_post.call_args.args[0]
        assert url == "https://broker.example.com/internal/delete-run-token"
        body = mock_post.call_args.kwargs["json"]
        assert body["broker_token"] == "tok-abc"
        assert body["run_id"] == "run-001"
        assert mock_post.call_args.kwargs["headers"]["Authorization"] == "Bearer svc-token-xyz"

    def test_raises_broker_error_on_401(self):
        mock_response = MagicMock()
        mock_response.status_code = 401

        with patch("pmf_engine.control_plane.broker_client.httpx.post", return_value=mock_response):
            client = BrokerClient("https://broker.example.com", "bad-token")
            with pytest.raises(BrokerError) as exc_info:
                client.delete_run_token("tok-abc", "run-001")
            assert exc_info.value.status_code == 401

    def test_accepts_204_as_success(self):
        mock_response = MagicMock()
        mock_response.status_code = 204
        with patch("pmf_engine.control_plane.broker_client.httpx.post", return_value=mock_response):
            client = BrokerClient("https://broker.example.com", "tok")
            client.delete_run_token("tok-abc", "run-001")


class TestMintRunTokenSuccess:
    def test_returns_dict_with_broker_token(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "broker_token": "tok-abc123",
            "exp": 1700000000,
            "params_clean": {"city": "Hendersonville"},
        }

        with patch("pmf_engine.control_plane.broker_client.httpx.post", return_value=mock_response) as mock_post:
            client = BrokerClient("https://broker.example.com", "svc-token-xyz")
            result = client.mint_run_token(
                run_id="run-001",
                organization_slug="org-123",
                experiment_id="smoke_test",
                scope={"state": "NC", "cities": ["Hendersonville"]},
                params={"city": "Hendersonville"},
                clerk_user_id="user_test",
                exp_ttl_seconds=3600,
            )

        assert result["broker_token"] == "tok-abc123"
        assert result["exp"] == 1700000000
        assert result["params_clean"] == {"city": "Hendersonville"}

        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        assert call_kwargs.kwargs["json"]["run_id"] == "run-001"
        assert call_kwargs.kwargs["json"]["organization_slug"] == "org-123"
        assert call_kwargs.kwargs["headers"]["Authorization"] == "Bearer svc-token-xyz"

    def test_strips_trailing_slash_from_broker_url(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"broker_token": "tok-abc"}

        with patch("pmf_engine.control_plane.broker_client.httpx.post", return_value=mock_response) as mock_post:
            client = BrokerClient("https://broker.example.com/", "token")
            client.mint_run_token("run-1", "org-1", "exp-1", {}, {}, "user_test")

        url = mock_post.call_args.args[0]
        assert url == "https://broker.example.com/internal/mint-run-token"


class TestMintRunToken401:
    def test_raises_broker_error_on_401(self):
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.raise_for_status = MagicMock()

        with patch("pmf_engine.control_plane.broker_client.httpx.post", return_value=mock_response):
            client = BrokerClient("https://broker.example.com", "bad-token")

            with pytest.raises(BrokerError) as exc_info:
                client.mint_run_token("run-1", "org-1", "exp-1", {}, {}, "user_test")

            assert exc_info.value.status_code == 401
            assert "service token" in exc_info.value.detail.lower()


class TestMintRunToken400:
    def test_raises_broker_error_with_user_safe_message(self):
        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.json.return_value = {
            "detail": "Param classifier rejected: nested objects",
            "user_safe_message": "Invalid experiment parameters",
        }

        with patch("pmf_engine.control_plane.broker_client.httpx.post", return_value=mock_response):
            client = BrokerClient("https://broker.example.com", "svc-token")

            with pytest.raises(BrokerError) as exc_info:
                client.mint_run_token("run-1", "org-1", "exp-1", {}, {"bad": {"nested": True}}, "user_test")

            assert exc_info.value.status_code == 400
            assert exc_info.value.user_safe_message == "Invalid experiment parameters"
            assert "classifier" in exc_info.value.detail.lower()


class TestMintRunToken409:
    def test_raises_broker_error_on_duplicate(self):
        mock_response = MagicMock()
        mock_response.status_code = 409
        mock_response.raise_for_status = MagicMock()

        with patch("pmf_engine.control_plane.broker_client.httpx.post", return_value=mock_response):
            client = BrokerClient("https://broker.example.com", "svc-token")

            with pytest.raises(BrokerError) as exc_info:
                client.mint_run_token("run-dup", "org-1", "exp-1", {}, {}, "user_test")

            assert exc_info.value.status_code == 409
            assert "duplicate" in exc_info.value.detail.lower()


class TestMintRunTokenPriorArtifactVersions:
    def test_mint_run_token_forwards_prior_artifact_versions(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"broker_token": "tok-xyz"}

        with patch("pmf_engine.control_plane.broker_client.httpx.post", return_value=mock_response) as mock_post:
            client = BrokerClient("https://broker.example.com", "svc-token")
            client.mint_run_token(
                run_id="run-peer-001",
                organization_slug="org-1",
                experiment_id="smoke_main",
                scope={"state": "NC"},
                params={"issues": ["housing"]},
                clerk_user_id="user_test",
                prior_artifact_versions={
                    "smoke_dep": "smoke_dep/org-1/run-a/artifact.json"
                },
            )

        body = mock_post.call_args.kwargs["json"]
        assert "prior_artifact_versions" in body
        assert body["prior_artifact_versions"] == {
            "smoke_dep": "smoke_dep/org-1/run-a/artifact.json"
        }

    def test_mint_run_token_omits_prior_artifact_versions_when_none(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"broker_token": "tok-xyz"}

        with patch("pmf_engine.control_plane.broker_client.httpx.post", return_value=mock_response) as mock_post:
            client = BrokerClient("https://broker.example.com", "svc-token")
            client.mint_run_token(
                run_id="run-001",
                organization_slug="org-1",
                experiment_id="smoke_test",
                scope={"state": "NC"},
                params={"city": "Hendersonville"},
                clerk_user_id="user_test",
            )

        body = mock_post.call_args.kwargs["json"]
        assert body.get("prior_artifact_versions") is None
