import os
import threading
import pytest
from unittest.mock import MagicMock, patch

from shared.braintrust import (
    BraintrustClient,
    init_braintrust,
    traced_llm_call,
    load_prompt_from_braintrust,
    flush_logs,
    is_enabled,
    get_client,
)


@pytest.fixture(autouse=True)
def reset_braintrust():
    BraintrustClient.reset_instance()
    yield
    BraintrustClient.reset_instance()


@pytest.fixture
def no_api_key(monkeypatch):
    monkeypatch.delenv("BRAINTRUST_API_KEY", raising=False)


class TestInitBraintrust:
    def test_project_is_required(self):
        with pytest.raises(TypeError):
            init_braintrust()

    def test_disabled_without_api_key(self, no_api_key):
        result = init_braintrust(project="test-project")
        assert result is False
        assert is_enabled() is False

    def test_enabled_with_api_key(self, monkeypatch):
        monkeypatch.setenv("BRAINTRUST_API_KEY", "test-key")

        mock_logger = MagicMock()
        mock_braintrust = MagicMock()
        mock_braintrust.init_logger.return_value = mock_logger

        with patch.dict("sys.modules", {"braintrust": mock_braintrust}):
            BraintrustClient.reset_instance()
            result = init_braintrust(project="test-project")

            assert result is True
            assert is_enabled() is True
            mock_braintrust.init_logger.assert_called_once_with(
                project="test-project",
                api_key="test-key"
            )

    def test_singleton_ignores_different_project(self, no_api_key):
        init_braintrust(project="project-1")
        init_braintrust(project="project-2")

        assert get_client().get_project() == "project-1"

    def test_get_project_returns_configured_project(self, no_api_key):
        init_braintrust(project="my-project")
        assert get_client().get_project() == "my-project"


class TestTracedLlmCall:
    def test_passthrough_when_disabled(self, no_api_key):
        init_braintrust(project="test")

        result = traced_llm_call(
            name="test-call",
            input_data={"input": "test"},
            llm_call_fn=lambda: "llm-response"
        )

        assert result == "llm-response"

    def test_returns_llm_result_when_enabled(self, monkeypatch):
        monkeypatch.setenv("BRAINTRUST_API_KEY", "test-key")

        mock_span = MagicMock()
        mock_span.__enter__ = MagicMock(return_value=mock_span)
        mock_span.__exit__ = MagicMock(return_value=False)

        mock_logger = MagicMock()
        mock_logger.start_span.return_value = mock_span

        mock_braintrust = MagicMock()
        mock_braintrust.init_logger.return_value = mock_logger

        with patch.dict("sys.modules", {"braintrust": mock_braintrust}):
            BraintrustClient.reset_instance()
            init_braintrust(project="test")

            result = traced_llm_call(
                name="test-call",
                input_data={"input": "test"},
                llm_call_fn=lambda: {"response": "hello"}
            )

            assert result == {"response": "hello"}
            mock_logger.start_span.assert_called_once_with(name="test-call")
            mock_span.log.assert_called_once()


class TestLoadPromptFromBraintrust:
    def test_uses_fallback_when_disabled(self, no_api_key):
        init_braintrust(project="test")

        result = load_prompt_from_braintrust(
            prompt_name="test-prompt",
            fallback_prompt="Hello {name}!",
            variables={"name": "World"}
        )

        assert result == "Hello World!"

    def test_uses_fallback_without_variables(self, no_api_key):
        init_braintrust(project="test")

        result = load_prompt_from_braintrust(
            prompt_name="test-prompt",
            fallback_prompt="Static prompt"
        )

        assert result == "Static prompt"

    def test_handles_missing_variable_gracefully(self, no_api_key):
        init_braintrust(project="test")

        result = load_prompt_from_braintrust(
            prompt_name="test-prompt",
            fallback_prompt="Hello {name} and {missing}!",
            variables={"name": "World"}
        )

        assert result == "Hello {name} and {missing}!"

    def test_loads_prompt_from_braintrust_when_enabled(self, monkeypatch):
        monkeypatch.setenv("BRAINTRUST_API_KEY", "test-key")

        mock_prompt = MagicMock()
        mock_prompt.build.return_value = "Prompt from Braintrust with value: test-value"

        mock_logger = MagicMock()
        mock_braintrust = MagicMock()
        mock_braintrust.init_logger.return_value = mock_logger
        mock_braintrust.load_prompt.return_value = mock_prompt

        with patch.dict("sys.modules", {"braintrust": mock_braintrust}):
            BraintrustClient.reset_instance()
            init_braintrust(project="test-project")

            result = load_prompt_from_braintrust(
                prompt_name="my-prompt",
                fallback_prompt="Fallback {var}",
                variables={"var": "test-value"}
            )

            assert result == "Prompt from Braintrust with value: test-value"
            mock_braintrust.load_prompt.assert_called_once_with(
                project="test-project",
                slug="my-prompt"
            )
            mock_prompt.build.assert_called_once_with(var="test-value")

    def test_loads_prompt_returns_fallback_when_prompt_not_found(self, monkeypatch):
        monkeypatch.setenv("BRAINTRUST_API_KEY", "test-key")

        mock_logger = MagicMock()
        mock_braintrust = MagicMock()
        mock_braintrust.init_logger.return_value = mock_logger
        mock_braintrust.load_prompt.return_value = None

        with patch.dict("sys.modules", {"braintrust": mock_braintrust}):
            BraintrustClient.reset_instance()
            init_braintrust(project="test")

            result = load_prompt_from_braintrust(
                prompt_name="missing-prompt",
                fallback_prompt="Fallback: {name}",
                variables={"name": "World"}
            )

            assert result == "Fallback: World"

    def test_loads_prompt_handles_messages_response(self, monkeypatch):
        monkeypatch.setenv("BRAINTRUST_API_KEY", "test-key")

        mock_message1 = MagicMock()
        mock_message1.content = "System message"
        mock_message2 = {"content": "User message"}

        mock_rendered = MagicMock()
        mock_rendered.messages = [mock_message1, mock_message2]

        mock_prompt = MagicMock()
        mock_prompt.build.return_value = mock_rendered

        mock_logger = MagicMock()
        mock_braintrust = MagicMock()
        mock_braintrust.init_logger.return_value = mock_logger
        mock_braintrust.load_prompt.return_value = mock_prompt

        with patch.dict("sys.modules", {"braintrust": mock_braintrust}):
            BraintrustClient.reset_instance()
            init_braintrust(project="test")

            result = load_prompt_from_braintrust(
                prompt_name="chat-prompt",
                fallback_prompt="fallback"
            )

            assert result == "System message\nUser message"


class TestSerializeOutput:
    def test_serialize_none(self, no_api_key):
        init_braintrust(project="test")
        client = get_client()

        assert client._serialize_output(None) == {"result": None}

    def test_serialize_dict(self, no_api_key):
        init_braintrust(project="test")
        client = get_client()

        assert client._serialize_output({"key": "value"}) == {"key": "value"}

    def test_serialize_primitives(self, no_api_key):
        init_braintrust(project="test")
        client = get_client()

        assert client._serialize_output("string") == {"result": "string"}
        assert client._serialize_output(123) == {"result": 123}
        assert client._serialize_output(True) == {"result": True}

    def test_serialize_list(self, no_api_key):
        init_braintrust(project="test")
        client = get_client()

        assert client._serialize_output([1, 2, 3]) == {"result": [1, 2, 3]}

    def test_serialize_pydantic_model(self, no_api_key):
        from pydantic import BaseModel

        class TestModel(BaseModel):
            name: str
            value: int

        init_braintrust(project="test")
        client = get_client()

        model = TestModel(name="test", value=123)
        assert client._serialize_output(model) == {"name": "test", "value": 123}


class TestEdgeCases:
    def test_traced_call_propagates_exception(self, no_api_key):
        init_braintrust(project="test")

        with pytest.raises(ValueError, match="LLM error"):
            traced_llm_call(
                name="test-call",
                input_data={},
                llm_call_fn=lambda: (_ for _ in ()).throw(ValueError("LLM error"))
            )

    def test_traced_call_returns_result_even_if_logging_fails(self, monkeypatch):
        monkeypatch.setenv("BRAINTRUST_API_KEY", "test-key")

        mock_span = MagicMock()
        mock_span.__enter__ = MagicMock(return_value=mock_span)
        mock_span.__exit__ = MagicMock(return_value=False)
        mock_span.log.side_effect = Exception("Logging failed")

        mock_logger = MagicMock()
        mock_logger.start_span.return_value = mock_span

        mock_braintrust = MagicMock()
        mock_braintrust.init_logger.return_value = mock_logger

        with patch.dict("sys.modules", {"braintrust": mock_braintrust}):
            BraintrustClient.reset_instance()
            init_braintrust(project="test")

            result = traced_llm_call(
                name="test-call",
                input_data={},
                llm_call_fn=lambda: "success"
            )

            assert result == "success"

    def test_graceful_when_braintrust_not_installed(self, monkeypatch):
        monkeypatch.setenv("BRAINTRUST_API_KEY", "test-key")

        with patch.dict("sys.modules", {"braintrust": None}):
            BraintrustClient.reset_instance()
            client = BraintrustClient()
            result = client.init(project="test")

            assert result is False
            assert client.is_enabled() is False

    def test_prompt_with_literal_braces(self, no_api_key):
        init_braintrust(project="test")

        result = load_prompt_from_braintrust(
            prompt_name="test",
            fallback_prompt="Use {{double braces}} for literal and {name} for var",
            variables={"name": "test"}
        )

        assert result == "Use {double braces} for literal and test for var"

    def test_reset_allows_reinitialization(self, no_api_key):
        init_braintrust(project="project-1")
        assert get_client().get_project() == "project-1"

        BraintrustClient.reset_instance()

        init_braintrust(project="project-2")
        assert get_client().get_project() == "project-2"

    def test_empty_input_data(self, no_api_key):
        init_braintrust(project="test")

        result = traced_llm_call(
            name="test",
            input_data={},
            llm_call_fn=lambda: "result"
        )

        assert result == "result"

    def test_none_metadata_and_tags(self, no_api_key):
        init_braintrust(project="test")

        result = traced_llm_call(
            name="test",
            input_data={"x": 1},
            llm_call_fn=lambda: "result",
            metadata=None,
            tags=None
        )

        assert result == "result"


class TestFlushLogs:
    def test_flush_noop_when_disabled(self, no_api_key):
        init_braintrust(project="test")
        flush_logs()

    def test_flush_calls_logger_flush(self, monkeypatch):
        monkeypatch.setenv("BRAINTRUST_API_KEY", "test-key")

        mock_logger = MagicMock()
        mock_braintrust = MagicMock()
        mock_braintrust.init_logger.return_value = mock_logger

        with patch.dict("sys.modules", {"braintrust": mock_braintrust}):
            BraintrustClient.reset_instance()
            init_braintrust(project="test")

            flush_logs()

            mock_logger.flush.assert_called_once()


class TestThreadSafety:
    def test_concurrent_init_returns_same_instance(self, no_api_key):
        instances = []
        errors = []

        def get_instance():
            try:
                init_braintrust(project="test")
                instances.append(get_client())
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=get_instance) for _ in range(10)]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert len(instances) == 10
        assert all(inst is instances[0] for inst in instances)

    def test_concurrent_traced_calls(self, no_api_key):
        init_braintrust(project="test")

        results = []
        errors = []

        def make_traced_call(i):
            try:
                result = traced_llm_call(
                    name=f"call-{i}",
                    input_data={"i": i},
                    llm_call_fn=lambda: f"result-{i}"
                )
                results.append(result)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=make_traced_call, args=(i,)) for i in range(20)]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert len(results) == 20
