import os
import threading
import pytest
from unittest.mock import MagicMock, patch, call

from shared.braintrust import (
    BraintrustClient,
    NoOpSpan,
    init_braintrust,
    traced_llm_call,
    load_prompt_from_braintrust,
    flush_logs,
    is_enabled,
    get_client,
)


class FakeSpan:
    def __init__(self, name: str):
        self.name = name
        self.inputs: list[dict] = []
        self.outputs: list[dict] = []
        self.metadatas: list[list] = []
        self.tags: list[list] = []
        self.enter_count = 0
        self.exit_count = 0
        self.exit_exc_infos: list[tuple] = []

    def log(self, input=None, output=None, metadata=None, tags=None, **kwargs):
        if input is not None:
            self.inputs.append(input)
        if output is not None:
            self.outputs.append(output)
        if metadata is not None:
            self.metadatas.append(metadata)
        if tags is not None:
            self.tags.append(tags)

    def __enter__(self):
        self.enter_count += 1
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.exit_count += 1
        self.exit_exc_infos.append((exc_type, exc_val, exc_tb))
        return False


class FakeBraintrustLogger:
    def __init__(self):
        self.spans: list[FakeSpan] = []
        self.flush_count = 0

    def start_span(self, name):
        span = FakeSpan(name)
        self.spans.append(span)
        return span

    def flush(self):
        self.flush_count += 1

    def span_named(self, name: str) -> FakeSpan:
        matches = [s for s in self.spans if s.name == name]
        assert matches, f"no span named {name!r}, have {[s.name for s in self.spans]}"
        return matches[-1]


class FakeBraintrustModule:
    def __init__(self):
        self.logger = FakeBraintrustLogger()
        self.init_calls: list[dict] = []
        self.load_prompt_calls: list[dict] = []
        self.prompt_to_return = None

    def init_logger(self, project=None, api_key=None):
        self.init_calls.append({"project": project, "api_key": api_key})
        return self.logger

    def load_prompt(self, project=None, slug=None):
        self.load_prompt_calls.append({"project": project, "slug": slug})
        return self.prompt_to_return


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
        fake_braintrust = FakeBraintrustModule()

        with patch.dict("sys.modules", {"braintrust": fake_braintrust}):
            BraintrustClient.reset_instance()
            init_braintrust(project="test")

            result = traced_llm_call(
                name="test-call",
                input_data={"input": "test"},
                llm_call_fn=lambda: {"response": "hello"}
            )

        assert result == {"response": "hello"}
        span = fake_braintrust.logger.span_named("test-call")
        assert span.inputs == [{"input": "test"}]
        assert span.outputs == [{"response": "hello"}]
        assert span.tags == [[]]
        assert span.metadatas == [{}]


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

        assert result == "Hello World and {missing}!"

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

        assert result == "Use {{double braces}} for literal and test for var"

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


class TestNoOpSpan:
    def test_log_is_noop(self):
        span = NoOpSpan()
        span.log(input={"x": 1}, output={"y": 2}, metadata={"k": "v"}, tags=["t"])

    def test_start_span_returns_noop_span(self):
        span = NoOpSpan()
        child = span.start_span(name="child")
        assert isinstance(child, NoOpSpan)

    def test_noop_span_works_as_context_manager(self):
        span = NoOpSpan()
        with span as s:
            assert isinstance(s, NoOpSpan)
            s.log(input={"data": "test"})

    def test_nested_noop_spans(self):
        span = NoOpSpan()
        with span.start_span(name="parent") as parent:
            with parent.start_span(name="child") as child:
                child.log(output={"result": "ok"})


class TestTracedSpan:
    def test_returns_noop_span_when_disabled(self, no_api_key):
        init_braintrust(project="test")
        client = get_client()

        with client.traced_span(name="test-span") as span:
            assert isinstance(span, NoOpSpan)

    def test_returns_real_span_when_enabled(self, monkeypatch):
        monkeypatch.setenv("BRAINTRUST_API_KEY", "test-key")
        fake_braintrust = FakeBraintrustModule()

        with patch.dict("sys.modules", {"braintrust": fake_braintrust}):
            BraintrustClient.reset_instance()
            init_braintrust(project="test")

            with get_client().traced_span(name="my-span") as span:
                assert isinstance(span, FakeSpan)
                assert span.name == "my-span"

        assert len(fake_braintrust.logger.spans) == 1
        assert fake_braintrust.logger.spans[0].exit_count == 1

    def test_logs_initial_input_data(self, monkeypatch):
        monkeypatch.setenv("BRAINTRUST_API_KEY", "test-key")
        fake_braintrust = FakeBraintrustModule()

        with patch.dict("sys.modules", {"braintrust": fake_braintrust}):
            BraintrustClient.reset_instance()
            init_braintrust(project="test")

            with get_client().traced_span(
                name="my-span",
                input_data={"params": {"state": "NC"}},
                metadata={"experiment": "voter_targeting"},
                tags=["pmf"],
            ):
                pass

        span = fake_braintrust.logger.span_named("my-span")
        assert span.inputs == [{"params": {"state": "NC"}}]
        assert span.metadatas == [{"experiment": "voter_targeting"}]
        assert span.tags == [["pmf"]]

    def test_caller_can_log_output(self, monkeypatch):
        monkeypatch.setenv("BRAINTRUST_API_KEY", "test-key")
        fake_braintrust = FakeBraintrustModule()

        with patch.dict("sys.modules", {"braintrust": fake_braintrust}):
            BraintrustClient.reset_instance()
            init_braintrust(project="test")

            with get_client().traced_span(name="my-span") as span:
                span.log(output={"artifact": "s3://bucket/key"})

        recorded = fake_braintrust.logger.span_named("my-span")
        assert recorded.outputs == [{"artifact": "s3://bucket/key"}]

    def test_handles_span_creation_failure(self, monkeypatch):
        monkeypatch.setenv("BRAINTRUST_API_KEY", "test-key")

        mock_logger = MagicMock()
        mock_logger.start_span.side_effect = Exception("Braintrust down")

        mock_braintrust = MagicMock()
        mock_braintrust.init_logger.return_value = mock_logger

        with patch.dict("sys.modules", {"braintrust": mock_braintrust}):
            BraintrustClient.reset_instance()
            init_braintrust(project="test")

            with get_client().traced_span(name="my-span") as span:
                assert isinstance(span, NoOpSpan)


    def test_no_initial_log_when_no_optional_args(self, monkeypatch):
        monkeypatch.setenv("BRAINTRUST_API_KEY", "test-key")
        fake_braintrust = FakeBraintrustModule()

        with patch.dict("sys.modules", {"braintrust": fake_braintrust}):
            BraintrustClient.reset_instance()
            init_braintrust(project="test")

            with get_client().traced_span(name="my-span"):
                pass

        span = fake_braintrust.logger.span_named("my-span")
        assert span.inputs == []
        assert span.outputs == []
        assert span.metadatas == []
        assert span.tags == []

    def test_caller_exception_propagates(self, monkeypatch):
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

            with pytest.raises(ValueError, match="caller error"):
                with get_client().traced_span(name="my-span") as span:
                    raise ValueError("caller error")

    def test_span_creation_failure_still_runs_body(self, monkeypatch):
        monkeypatch.setenv("BRAINTRUST_API_KEY", "test-key")

        mock_logger = MagicMock()
        mock_logger.start_span.side_effect = Exception("Braintrust down")

        mock_braintrust = MagicMock()
        mock_braintrust.init_logger.return_value = mock_logger

        with patch.dict("sys.modules", {"braintrust": mock_braintrust}):
            BraintrustClient.reset_instance()
            init_braintrust(project="test")

            body_ran = False
            with get_client().traced_span(name="my-span") as span:
                assert isinstance(span, NoOpSpan)
                span.log(output={"test": "data"})
                body_ran = True

            assert body_ran


class TestTracedCallExecutesInsideSpan:
    def test_callable_runs_inside_span_context(self, monkeypatch):
        monkeypatch.setenv("BRAINTRUST_API_KEY", "test-key")

        call_order = []

        mock_span = MagicMock()
        mock_span.__enter__ = MagicMock(side_effect=lambda: (call_order.append("span_enter"), mock_span)[1])
        mock_span.__exit__ = MagicMock(side_effect=lambda *a: call_order.append("span_exit"))
        mock_span.log = MagicMock()

        mock_logger = MagicMock()
        mock_logger.start_span.return_value = mock_span

        mock_braintrust = MagicMock()
        mock_braintrust.init_logger.return_value = mock_logger

        def tracked_fn():
            call_order.append("llm_call")
            return "result"

        with patch.dict("sys.modules", {"braintrust": mock_braintrust}):
            BraintrustClient.reset_instance()
            init_braintrust(project="test")

            result = traced_llm_call(
                name="test",
                input_data={"q": "hello"},
                llm_call_fn=tracked_fn,
            )

            assert result == "result"
            assert call_order == ["span_enter", "llm_call", "span_exit"]

    def test_llm_call_not_doubled_when_logging_fails(self, monkeypatch):
        monkeypatch.setenv("BRAINTRUST_API_KEY", "test-key")

        call_count = 0

        mock_span = MagicMock()
        mock_span.__enter__ = MagicMock(return_value=mock_span)
        mock_span.__exit__ = MagicMock(return_value=False)
        mock_span.log = MagicMock(side_effect=Exception("logging broken"))

        mock_logger = MagicMock()
        mock_logger.start_span.return_value = mock_span

        mock_braintrust = MagicMock()
        mock_braintrust.init_logger.return_value = mock_logger

        def expensive_llm_call():
            nonlocal call_count
            call_count += 1
            return "result"

        with patch.dict("sys.modules", {"braintrust": mock_braintrust}):
            BraintrustClient.reset_instance()
            init_braintrust(project="test")

            result = traced_llm_call(
                name="test",
                input_data={"q": "hello"},
                llm_call_fn=expensive_llm_call,
            )

            assert result == "result"
            assert call_count == 1

    def test_span_closed_when_llm_call_raises(self, monkeypatch):
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

            with pytest.raises(RuntimeError, match="LLM exploded"):
                traced_llm_call(
                    name="test",
                    input_data={"q": "hello"},
                    llm_call_fn=lambda: (_ for _ in ()).throw(RuntimeError("LLM exploded")),
                )

            mock_span.__exit__.assert_called_once()

    def test_span_exit_receives_exception_info_on_error(self, monkeypatch):
        monkeypatch.setenv("BRAINTRUST_API_KEY", "test-key")

        exit_calls = []

        mock_span = MagicMock()
        mock_span.__enter__ = MagicMock(return_value=mock_span)
        mock_span.__exit__ = MagicMock(
            side_effect=lambda exc_type, exc_val, exc_tb: exit_calls.append(
                (exc_type, exc_val, exc_tb)
            )
        )

        mock_logger = MagicMock()
        mock_logger.start_span.return_value = mock_span

        mock_braintrust = MagicMock()
        mock_braintrust.init_logger.return_value = mock_logger

        def boom():
            raise ValueError("boom")

        with patch.dict("sys.modules", {"braintrust": mock_braintrust}):
            BraintrustClient.reset_instance()
            init_braintrust(project="test")

            with pytest.raises(ValueError, match="boom"):
                traced_llm_call(
                    name="test",
                    input_data={"q": "hello"},
                    llm_call_fn=boom,
                )

            assert len(exit_calls) == 1
            exc_type, exc_val, exc_tb = exit_calls[0]
            assert exc_type is ValueError
            assert isinstance(exc_val, ValueError)
            assert exc_val.args == ("boom",)
            assert exc_tb is not None

    def test_span_exit_receives_none_triple_on_success(self, monkeypatch):
        monkeypatch.setenv("BRAINTRUST_API_KEY", "test-key")

        exit_calls = []

        mock_span = MagicMock()
        mock_span.__enter__ = MagicMock(return_value=mock_span)
        mock_span.__exit__ = MagicMock(
            side_effect=lambda exc_type, exc_val, exc_tb: exit_calls.append(
                (exc_type, exc_val, exc_tb)
            )
        )

        mock_logger = MagicMock()
        mock_logger.start_span.return_value = mock_span

        mock_braintrust = MagicMock()
        mock_braintrust.init_logger.return_value = mock_logger

        with patch.dict("sys.modules", {"braintrust": mock_braintrust}):
            BraintrustClient.reset_instance()
            init_braintrust(project="test")

            result = traced_llm_call(
                name="test",
                input_data={"q": "hello"},
                llm_call_fn=lambda: "ok",
            )

            assert result == "ok"
            assert exit_calls == [(None, None, None)]
