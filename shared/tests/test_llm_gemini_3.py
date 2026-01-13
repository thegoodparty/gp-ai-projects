import pytest
from unittest.mock import Mock, patch
from pydantic import BaseModel

from shared.llm_gemini_3 import (
    Gemini3Client,
    GeminiModelType,
    ThinkingLevel,
    GEMINI_3_PRICING,
)


class TestEnums:
    def test_model_types(self):
        assert GeminiModelType.FLASH_3.value == "gemini-3-flash-preview"
        assert GeminiModelType.PRO_3.value == "gemini-3-pro-preview"

    def test_thinking_levels(self):
        assert ThinkingLevel.MINIMAL.value == "minimal"
        assert ThinkingLevel.LOW.value == "low"
        assert ThinkingLevel.MEDIUM.value == "medium"
        assert ThinkingLevel.HIGH.value == "high"


class TestPricing:
    def test_flash_3_pricing(self):
        pricing = GEMINI_3_PRICING['gemini-3-flash-preview']
        assert pricing['input'] == 0.50
        assert pricing['output'] == 3.00

    def test_pro_3_pricing(self):
        pricing = GEMINI_3_PRICING['gemini-3-pro-preview']
        assert pricing['input'] == 2.50
        assert pricing['output'] == 15.00


class TestGemini3ClientInit:
    @patch('shared.llm_gemini_3.genai.Client')
    def test_default_init(self, _mock_genai):
        client = Gemini3Client(api_key="test-key")

        assert client.default_model == GeminiModelType.FLASH_3
        assert client.default_temperature == 0.7
        assert client.thinking_level == ThinkingLevel.MINIMAL
        assert client.include_thoughts is False
        assert client.max_retries == 3
        assert client.api_call_count == 0
        assert client.total_cost == 0.0

    @patch('shared.llm_gemini_3.genai.Client')
    def test_custom_init(self, _mock_genai):
        client = Gemini3Client(
            api_key="test-key",
            default_model=GeminiModelType.PRO_3,
            default_temperature=0.5,
            thinking_level=ThinkingLevel.HIGH,
            include_thoughts=True,
            max_retries=5
        )

        assert client.default_model == GeminiModelType.PRO_3
        assert client.default_temperature == 0.5
        assert client.thinking_level == ThinkingLevel.HIGH
        assert client.include_thoughts is True
        assert client.max_retries == 5

    def test_missing_api_key_raises(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        with pytest.raises(ValueError, match="GEMINI_API_KEY is required"):
            Gemini3Client(api_key=None)


class TestBuildConfig:
    @patch('shared.llm_gemini_3.genai.Client')
    def test_default_config(self, _mock_genai):
        client = Gemini3Client(api_key="test-key")
        config = client._build_config(GeminiModelType.FLASH_3)

        assert config.temperature == 0.7
        assert "minimal" in str(config.thinking_config.thinking_level).lower()
        assert config.thinking_config.include_thoughts is False

    @patch('shared.llm_gemini_3.genai.Client')
    def test_override_thinking_level(self, _mock_genai):
        client = Gemini3Client(api_key="test-key")
        config = client._build_config(GeminiModelType.FLASH_3, thinking_level=ThinkingLevel.HIGH)

        assert "high" in str(config.thinking_config.thinking_level).lower()

    @patch('shared.llm_gemini_3.genai.Client')
    def test_override_temperature(self, _mock_genai):
        client = Gemini3Client(api_key="test-key")
        config = client._build_config(GeminiModelType.FLASH_3, temperature=0.3)

        assert config.temperature == 0.3

    @patch('shared.llm_gemini_3.genai.Client')
    def test_override_include_thoughts(self, _mock_genai):
        client = Gemini3Client(api_key="test-key")
        config = client._build_config(GeminiModelType.FLASH_3, include_thoughts=True)

        assert config.thinking_config.include_thoughts is True

    @patch('shared.llm_gemini_3.genai.Client')
    def test_pro3_minimal_raises_error(self, _mock_genai):
        client = Gemini3Client(api_key="test-key", thinking_level=ThinkingLevel.MINIMAL)

        with pytest.raises(ValueError, match="PRO_3 model does not support MINIMAL"):
            client._build_config(GeminiModelType.PRO_3)

    @patch('shared.llm_gemini_3.genai.Client')
    def test_pro3_low_works(self, _mock_genai):
        client = Gemini3Client(api_key="test-key", thinking_level=ThinkingLevel.LOW)
        config = client._build_config(GeminiModelType.PRO_3)

        assert "low" in str(config.thinking_config.thinking_level).lower()


class TestCostCalculation:
    @patch('shared.llm_gemini_3.genai.Client')
    def test_flash_cost_per_million(self, _mock_genai):
        client = Gemini3Client(api_key="test-key")
        cost = client._calculate_cost("gemini-3-flash-preview", 1_000_000, 1_000_000)

        assert cost == 0.50 + 3.00

    @patch('shared.llm_gemini_3.genai.Client')
    def test_pro_cost_per_million(self, _mock_genai):
        client = Gemini3Client(api_key="test-key")
        cost = client._calculate_cost("gemini-3-pro-preview", 1_000_000, 1_000_000)

        assert cost == 2.50 + 15.00

    @patch('shared.llm_gemini_3.genai.Client')
    def test_small_token_cost(self, _mock_genai):
        client = Gemini3Client(api_key="test-key")
        cost = client._calculate_cost("gemini-3-flash-preview", 1000, 500)

        expected = (1000 / 1_000_000) * 0.50 + (500 / 1_000_000) * 3.00
        assert abs(cost - expected) < 0.0001

    @patch('shared.llm_gemini_3.genai.Client')
    def test_unknown_model_defaults_to_flash(self, _mock_genai):
        client = Gemini3Client(api_key="test-key")
        cost = client._calculate_cost("unknown-model", 1_000_000, 1_000_000)

        assert cost == 0.50 + 3.00


class TestUsageTracking:
    @patch('shared.llm_gemini_3.genai.Client')
    def test_update_usage_tracks_tokens(self, _mock_genai):
        client = Gemini3Client(api_key="test-key")

        mock_response = Mock()
        mock_response.usage_metadata = Mock()
        mock_response.usage_metadata.prompt_token_count = 100
        mock_response.usage_metadata.candidates_token_count = 50

        client._update_usage("gemini-3-flash-preview", mock_response)

        assert client.api_call_count == 1
        assert client.total_prompt_tokens == 100
        assert client.total_completion_tokens == 50
        assert client.total_cost > 0

    @patch('shared.llm_gemini_3.genai.Client')
    def test_update_usage_handles_missing_metadata(self, _mock_genai):
        client = Gemini3Client(api_key="test-key")

        mock_response = Mock()
        mock_response.usage_metadata = None

        client._update_usage("gemini-3-flash-preview", mock_response)

        assert client.api_call_count == 1
        assert client.total_prompt_tokens == 0

    @patch('shared.llm_gemini_3.genai.Client')
    def test_get_usage_stats(self, _mock_genai):
        client = Gemini3Client(api_key="test-key")
        client.api_call_count = 10
        client.total_prompt_tokens = 5000
        client.total_completion_tokens = 2000
        client.total_cost = 0.05

        stats = client.get_usage_stats()

        assert stats["api_calls"] == 10
        assert stats["prompt_tokens"] == 5000
        assert stats["completion_tokens"] == 2000
        assert stats["total_cost"] == 0.05


class SampleResponse(BaseModel):
    answer: str
    confidence: float


class TestGenerateStructuredContent:
    @patch('shared.llm_gemini_3.genai.Client')
    def test_returns_pydantic_model(self, _mock_genai):
        client = Gemini3Client(api_key="test-key")

        mock_response = Mock()
        mock_response.text = '{"answer": "test answer", "confidence": 0.95}'
        mock_response.usage_metadata = Mock()
        mock_response.usage_metadata.prompt_token_count = 100
        mock_response.usage_metadata.candidates_token_count = 50

        client.client.models.generate_content = Mock(return_value=mock_response)

        result = client.generate_structured_content(
            prompt="Test prompt",
            response_schema=SampleResponse
        )

        assert isinstance(result, SampleResponse)
        assert result.answer == "test answer"
        assert result.confidence == 0.95

    @patch('shared.llm_gemini_3.genai.Client')
    def test_returns_list_of_models(self, _mock_genai):
        client = Gemini3Client(api_key="test-key")

        mock_response = Mock()
        mock_response.text = '[{"answer": "a1", "confidence": 0.9}, {"answer": "a2", "confidence": 0.8}]'
        mock_response.usage_metadata = Mock()
        mock_response.usage_metadata.prompt_token_count = 100
        mock_response.usage_metadata.candidates_token_count = 50

        client.client.models.generate_content = Mock(return_value=mock_response)

        result = client.generate_structured_content(
            prompt="Test prompt",
            response_schema=SampleResponse
        )

        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0].answer == "a1"

    @patch('shared.llm_gemini_3.genai.Client')
    def test_passes_system_instruction(self, _mock_genai):
        client = Gemini3Client(api_key="test-key")

        mock_response = Mock()
        mock_response.text = '{"answer": "test", "confidence": 0.9}'
        mock_response.usage_metadata = Mock()
        mock_response.usage_metadata.prompt_token_count = 100
        mock_response.usage_metadata.candidates_token_count = 50

        client.client.models.generate_content = Mock(return_value=mock_response)

        client.generate_structured_content(
            prompt="Test",
            response_schema=SampleResponse,
            system_instruction="Be helpful"
        )

        call_args = client.client.models.generate_content.call_args
        assert call_args.kwargs['config'].system_instruction == "Be helpful"


class TestGenerateContent:
    @patch('shared.llm_gemini_3.genai.Client')
    def test_returns_text(self, _mock_genai):
        client = Gemini3Client(api_key="test-key")

        mock_response = Mock()
        mock_response.text = "This is the response"
        mock_response.usage_metadata = Mock()
        mock_response.usage_metadata.prompt_token_count = 50
        mock_response.usage_metadata.candidates_token_count = 20

        client.client.models.generate_content = Mock(return_value=mock_response)

        result = client.generate_content(prompt="Test prompt")

        assert result == "This is the response"
        assert client.api_call_count == 1

    @patch('shared.llm_gemini_3.genai.Client')
    def test_uses_specified_model(self, _mock_genai):
        client = Gemini3Client(api_key="test-key", thinking_level=ThinkingLevel.LOW)

        mock_response = Mock()
        mock_response.text = "Response"
        mock_response.usage_metadata = Mock()
        mock_response.usage_metadata.prompt_token_count = 50
        mock_response.usage_metadata.candidates_token_count = 20

        client.client.models.generate_content = Mock(return_value=mock_response)

        client.generate_content(prompt="Test", model=GeminiModelType.PRO_3)

        call_args = client.client.models.generate_content.call_args
        assert call_args.kwargs['model'] == "gemini-3-pro-preview"


class TestRetryLogic:
    @patch('shared.llm_gemini_3.genai.Client')
    @patch('shared.llm_gemini_3.time.sleep')
    def test_retries_on_failure(self, mock_sleep, _mock_genai):
        client = Gemini3Client(api_key="test-key", max_retries=3)

        mock_response = Mock()
        mock_response.text = "Success"
        mock_response.usage_metadata = Mock()
        mock_response.usage_metadata.prompt_token_count = 50
        mock_response.usage_metadata.candidates_token_count = 20

        client.client.models.generate_content = Mock(
            side_effect=[Exception("Error 1"), Exception("Error 2"), mock_response]
        )

        result = client.generate_content(prompt="Test")

        assert result == "Success"
        assert mock_sleep.call_count == 2

    @patch('shared.llm_gemini_3.genai.Client')
    @patch('shared.llm_gemini_3.time.sleep')
    def test_exponential_backoff(self, mock_sleep, _mock_genai):
        client = Gemini3Client(api_key="test-key", max_retries=3, base_delay=1.0)

        mock_response = Mock()
        mock_response.text = "Success"
        mock_response.usage_metadata = Mock()
        mock_response.usage_metadata.prompt_token_count = 50
        mock_response.usage_metadata.candidates_token_count = 20

        client.client.models.generate_content = Mock(
            side_effect=[Exception("Error"), Exception("Error"), mock_response]
        )

        client.generate_content(prompt="Test")

        assert mock_sleep.call_args_list[0][0][0] == 1.0
        assert mock_sleep.call_args_list[1][0][0] == 2.0

    @patch('shared.llm_gemini_3.genai.Client')
    @patch('shared.llm_gemini_3.time.sleep')
    def test_raises_after_max_retries(self, mock_sleep, _mock_genai):
        client = Gemini3Client(api_key="test-key", max_retries=3)

        client.client.models.generate_content = Mock(
            side_effect=Exception("Persistent error")
        )

        with pytest.raises(Exception, match="Persistent error"):
            client.generate_content(prompt="Test")

        assert mock_sleep.call_count == 2


class TestEmptyResponse:
    @patch('shared.llm_gemini_3.genai.Client')
    @patch('shared.llm_gemini_3.time.sleep')
    def test_raises_on_empty_text(self, mock_sleep, _mock_genai):
        client = Gemini3Client(api_key="test-key", max_retries=1)

        mock_response = Mock()
        mock_response.text = None
        mock_response.usage_metadata = None

        client.client.models.generate_content = Mock(return_value=mock_response)

        with pytest.raises(ValueError, match="Empty response"):
            client.generate_content(prompt="Test")

    @patch('shared.llm_gemini_3.genai.Client')
    @patch('shared.llm_gemini_3.time.sleep')
    def test_raises_on_empty_string(self, mock_sleep, _mock_genai):
        client = Gemini3Client(api_key="test-key", max_retries=1)

        mock_response = Mock()
        mock_response.text = ""
        mock_response.usage_metadata = None

        client.client.models.generate_content = Mock(return_value=mock_response)

        with pytest.raises(ValueError, match="Empty response"):
            client.generate_content(prompt="Test")


class TestBraintrustIntegration:
    @patch('shared.llm_gemini_3.genai.Client')
    @patch('shared.llm_gemini_3.braintrust_enabled')
    def test_skips_tracing_when_disabled(self, mock_bt_enabled, _mock_genai):
        mock_bt_enabled.return_value = False
        client = Gemini3Client(api_key="test-key")

        mock_response = Mock()
        mock_response.text = "Response"
        mock_response.usage_metadata = Mock()
        mock_response.usage_metadata.prompt_token_count = 50
        mock_response.usage_metadata.candidates_token_count = 20

        client.client.models.generate_content = Mock(return_value=mock_response)

        result = client.generate_content(prompt="Test", trace_name="my-trace")

        assert result == "Response"

    @patch('shared.llm_gemini_3.genai.Client')
    @patch('shared.llm_gemini_3.braintrust_enabled')
    @patch('shared.llm_gemini_3.get_braintrust_client')
    def test_traces_when_enabled(self, mock_get_bt, mock_bt_enabled, _mock_genai):
        mock_bt_enabled.return_value = True
        mock_bt_client = Mock()
        mock_bt_client.traced_call.return_value = "Traced response"
        mock_get_bt.return_value = mock_bt_client

        client = Gemini3Client(api_key="test-key")

        result = client.generate_content(prompt="Test prompt", trace_name="custom-trace")

        assert result == "Traced response"
        mock_bt_client.traced_call.assert_called_once()
        call_kwargs = mock_bt_client.traced_call.call_args.kwargs
        assert call_kwargs['name'] == "custom-trace"
        assert call_kwargs['input_data'] == {"prompt": "Test prompt"}

    @patch('shared.llm_gemini_3.genai.Client')
    @patch('shared.llm_gemini_3.braintrust_enabled')
    @patch('shared.llm_gemini_3.get_braintrust_client')
    def test_uses_default_trace_name(self, mock_get_bt, mock_bt_enabled, _mock_genai):
        mock_bt_enabled.return_value = True
        mock_bt_client = Mock()
        mock_bt_client.traced_call.return_value = "Response"
        mock_get_bt.return_value = mock_bt_client

        client = Gemini3Client(api_key="test-key")

        client.generate_content(prompt="Test")

        call_kwargs = mock_bt_client.traced_call.call_args.kwargs
        assert call_kwargs['name'] == "generate_content"

    @patch('shared.llm_gemini_3.genai.Client')
    @patch('shared.llm_gemini_3.braintrust_enabled')
    @patch('shared.llm_gemini_3.get_braintrust_client')
    def test_structured_content_traces(self, mock_get_bt, mock_bt_enabled, _mock_genai):
        mock_bt_enabled.return_value = True
        mock_bt_client = Mock()
        mock_bt_client.traced_call.return_value = SampleResponse(answer="test", confidence=0.9)
        mock_get_bt.return_value = mock_bt_client

        client = Gemini3Client(api_key="test-key")

        result = client.generate_structured_content(
            prompt="Test",
            response_schema=SampleResponse,
            trace_name="structured-trace"
        )

        call_kwargs = mock_bt_client.traced_call.call_args.kwargs
        assert call_kwargs['name'] == "structured-trace"


class TestInvalidJsonResponse:
    @patch('shared.llm_gemini_3.genai.Client')
    @patch('shared.llm_gemini_3.time.sleep')
    def test_raises_on_invalid_json(self, mock_sleep, _mock_genai):
        client = Gemini3Client(api_key="test-key", max_retries=1)

        mock_response = Mock()
        mock_response.text = "not valid json {"
        mock_response.usage_metadata = Mock()
        mock_response.usage_metadata.prompt_token_count = 50
        mock_response.usage_metadata.candidates_token_count = 20

        client.client.models.generate_content = Mock(return_value=mock_response)

        with pytest.raises(Exception):
            client.generate_structured_content(
                prompt="Test",
                response_schema=SampleResponse
            )

    @patch('shared.llm_gemini_3.genai.Client')
    @patch('shared.llm_gemini_3.time.sleep')
    def test_raises_on_schema_mismatch(self, mock_sleep, _mock_genai):
        client = Gemini3Client(api_key="test-key", max_retries=1)

        mock_response = Mock()
        mock_response.text = '{"wrong_field": "value"}'
        mock_response.usage_metadata = Mock()
        mock_response.usage_metadata.prompt_token_count = 50
        mock_response.usage_metadata.candidates_token_count = 20

        client.client.models.generate_content = Mock(return_value=mock_response)

        with pytest.raises(Exception):
            client.generate_structured_content(
                prompt="Test",
                response_schema=SampleResponse
            )


class TestCumulativeUsage:
    @patch('shared.llm_gemini_3.genai.Client')
    def test_accumulates_across_calls(self, _mock_genai):
        client = Gemini3Client(api_key="test-key")

        mock_response1 = Mock()
        mock_response1.text = "Response 1"
        mock_response1.usage_metadata = Mock()
        mock_response1.usage_metadata.prompt_token_count = 100
        mock_response1.usage_metadata.candidates_token_count = 50

        mock_response2 = Mock()
        mock_response2.text = "Response 2"
        mock_response2.usage_metadata = Mock()
        mock_response2.usage_metadata.prompt_token_count = 200
        mock_response2.usage_metadata.candidates_token_count = 100

        client.client.models.generate_content = Mock(
            side_effect=[mock_response1, mock_response2]
        )

        client.generate_content(prompt="First")
        client.generate_content(prompt="Second")

        assert client.api_call_count == 2
        assert client.total_prompt_tokens == 300
        assert client.total_completion_tokens == 150


class TestAllThinkingLevels:
    @patch('shared.llm_gemini_3.genai.Client')
    def test_minimal_level(self, _mock_genai):
        client = Gemini3Client(api_key="test-key", thinking_level=ThinkingLevel.MINIMAL)
        config = client._build_config(GeminiModelType.FLASH_3)
        assert "minimal" in str(config.thinking_config.thinking_level).lower()

    @patch('shared.llm_gemini_3.genai.Client')
    def test_low_level(self, _mock_genai):
        client = Gemini3Client(api_key="test-key", thinking_level=ThinkingLevel.LOW)
        config = client._build_config(GeminiModelType.FLASH_3)
        assert "low" in str(config.thinking_config.thinking_level).lower()

    @patch('shared.llm_gemini_3.genai.Client')
    def test_medium_level(self, _mock_genai):
        client = Gemini3Client(api_key="test-key", thinking_level=ThinkingLevel.MEDIUM)
        config = client._build_config(GeminiModelType.FLASH_3)
        assert "medium" in str(config.thinking_config.thinking_level).lower()

    @patch('shared.llm_gemini_3.genai.Client')
    def test_high_level(self, _mock_genai):
        client = Gemini3Client(api_key="test-key", thinking_level=ThinkingLevel.HIGH)
        config = client._build_config(GeminiModelType.FLASH_3)
        assert "high" in str(config.thinking_config.thinking_level).lower()


class TestZeroTokens:
    @patch('shared.llm_gemini_3.genai.Client')
    def test_handles_zero_prompt_tokens(self, _mock_genai):
        client = Gemini3Client(api_key="test-key")

        mock_response = Mock()
        mock_response.usage_metadata = Mock()
        mock_response.usage_metadata.prompt_token_count = 0
        mock_response.usage_metadata.candidates_token_count = 50

        client._update_usage("gemini-3-flash-preview", mock_response)

        assert client.total_prompt_tokens == 0
        assert client.total_completion_tokens == 50

    @patch('shared.llm_gemini_3.genai.Client')
    def test_handles_zero_completion_tokens(self, _mock_genai):
        client = Gemini3Client(api_key="test-key")

        mock_response = Mock()
        mock_response.usage_metadata = Mock()
        mock_response.usage_metadata.prompt_token_count = 100
        mock_response.usage_metadata.candidates_token_count = 0

        client._update_usage("gemini-3-flash-preview", mock_response)

        assert client.total_prompt_tokens == 100
        assert client.total_completion_tokens == 0

    @patch('shared.llm_gemini_3.genai.Client')
    def test_zero_cost_for_zero_tokens(self, _mock_genai):
        client = Gemini3Client(api_key="test-key")
        cost = client._calculate_cost("gemini-3-flash-preview", 0, 0)
        assert cost == 0.0


class TestEnvVarApiKey:
    @patch('shared.llm_gemini_3.genai.Client')
    def test_uses_env_var_when_no_explicit_key(self, _mock_genai, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "env-api-key")
        client = Gemini3Client()
        assert client.api_key == "env-api-key"


class TestConnectionConfig:
    @patch('shared.llm_gemini_3.genai.Client')
    def test_custom_connection_limits(self, mock_genai):
        client = Gemini3Client(
            api_key="test-key",
            max_connections=500,
            max_keepalive_connections=100
        )

        call_kwargs = mock_genai.call_args.kwargs
        http_options = call_kwargs['http_options']

        assert http_options.client_args['limits'].max_connections == 500
        assert http_options.client_args['limits'].max_keepalive_connections == 100


class TestBaseDelay:
    @patch('shared.llm_gemini_3.genai.Client')
    @patch('shared.llm_gemini_3.time.sleep')
    def test_custom_base_delay(self, mock_sleep, _mock_genai):
        client = Gemini3Client(api_key="test-key", max_retries=2, base_delay=2.0)

        mock_response = Mock()
        mock_response.text = "Success"
        mock_response.usage_metadata = Mock()
        mock_response.usage_metadata.prompt_token_count = 50
        mock_response.usage_metadata.candidates_token_count = 20

        client.client.models.generate_content = Mock(
            side_effect=[Exception("Error"), mock_response]
        )

        client.generate_content(prompt="Test")

        mock_sleep.assert_called_once_with(2.0)


class TestNoneTokenCounts:
    @patch('shared.llm_gemini_3.genai.Client')
    def test_handles_none_prompt_token_count(self, _mock_genai):
        client = Gemini3Client(api_key="test-key")

        mock_response = Mock()
        mock_response.usage_metadata = Mock()
        mock_response.usage_metadata.prompt_token_count = None
        mock_response.usage_metadata.candidates_token_count = 50

        client._update_usage("gemini-3-flash-preview", mock_response)

        assert client.total_prompt_tokens == 0
        assert client.total_completion_tokens == 50

    @patch('shared.llm_gemini_3.genai.Client')
    def test_handles_none_candidates_token_count(self, _mock_genai):
        client = Gemini3Client(api_key="test-key")

        mock_response = Mock()
        mock_response.usage_metadata = Mock()
        mock_response.usage_metadata.prompt_token_count = 100
        mock_response.usage_metadata.candidates_token_count = None

        client._update_usage("gemini-3-flash-preview", mock_response)

        assert client.total_prompt_tokens == 100
        assert client.total_completion_tokens == 0
