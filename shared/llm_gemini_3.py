import os
import json
import time
from typing import Optional, Type, Union, List, Dict, Any
from enum import Enum
from pydantic import BaseModel
from dotenv import load_dotenv
import httpx
from google import genai
from google.genai import types
from shared.logger import get_logger
from shared.braintrust import is_enabled as braintrust_enabled, get_client as get_braintrust_client

load_dotenv()

GEMINI_3_PRICING = {
    'gemini-3-flash-preview': {'input': 0.50, 'output': 3.00},
    'gemini-3-pro-preview': {'input': 2.50, 'output': 15.00},
}


class GeminiModelType(Enum):
    FLASH_3 = "gemini-3-flash-preview"
    PRO_3 = "gemini-3-pro-preview"


class ThinkingLevel(Enum):
    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Gemini3Client:
    def __init__(
        self,
        api_key: Optional[str] = None,
        default_model: GeminiModelType = GeminiModelType.FLASH_3,
        default_temperature: float = 0.7,
        thinking_level: ThinkingLevel = ThinkingLevel.MINIMAL,
        include_thoughts: bool = False,
        max_connections: int = 100,
        max_keepalive_connections: int = 20,
        max_retries: int = 3,
        base_delay: float = 1.0
    ):
        self.api_key = api_key or os.getenv("GEMINI_API_KEY")
        if not self.api_key:
            raise ValueError("GEMINI_API_KEY is required")

        self.default_model = default_model
        self.default_temperature = default_temperature
        self.thinking_level = thinking_level
        self.include_thoughts = include_thoughts
        self.max_retries = max_retries
        self.base_delay = base_delay

        http_options = types.HttpOptions(
            client_args={
                "limits": httpx.Limits(
                    max_connections=max_connections,
                    max_keepalive_connections=max_keepalive_connections
                ),
                "timeout": httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0)
            },
            async_client_args={
                "limits": httpx.Limits(
                    max_connections=max_connections,
                    max_keepalive_connections=max_keepalive_connections
                ),
                "timeout": httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0)
            }
        )

        self.client = genai.Client(api_key=self.api_key, http_options=http_options)
        self.logger = get_logger(__name__)

        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.total_cost = 0.0
        self.api_call_count = 0

        self.logger.info(f"Gemini3Client initialized: model={default_model.value}, thinking={thinking_level.value}, connections={max_connections}")

    def _build_config(
        self,
        model: GeminiModelType,
        temperature: Optional[float] = None,
        thinking_level: Optional[ThinkingLevel] = None,
        include_thoughts: Optional[bool] = None
    ) -> types.GenerateContentConfig:
        level = thinking_level or self.thinking_level
        thoughts = include_thoughts if include_thoughts is not None else self.include_thoughts

        if model == GeminiModelType.PRO_3 and level == ThinkingLevel.MINIMAL:
            raise ValueError("PRO_3 model does not support MINIMAL thinking level. Use LOW, MEDIUM, or HIGH instead.")

        level_map = {
            ThinkingLevel.MINIMAL: types.ThinkingLevel.MINIMAL,
            ThinkingLevel.LOW: types.ThinkingLevel.LOW,
            ThinkingLevel.MEDIUM: types.ThinkingLevel.MEDIUM,
            ThinkingLevel.HIGH: types.ThinkingLevel.HIGH,
        }

        return types.GenerateContentConfig(
            temperature=temperature if temperature is not None else self.default_temperature,
            thinking_config=types.ThinkingConfig(
                thinkingLevel=level_map[level],
                includeThoughts=thoughts
            )
        )

    def _calculate_cost(self, model_name: str, prompt_tokens: int, completion_tokens: int) -> float:
        pricing = GEMINI_3_PRICING.get(model_name, GEMINI_3_PRICING['gemini-3-flash-preview'])
        input_cost = (prompt_tokens / 1_000_000) * pricing['input']
        output_cost = (completion_tokens / 1_000_000) * pricing['output']
        return input_cost + output_cost

    def _update_usage(self, model_name: str, response):
        if hasattr(response, 'usage_metadata') and response.usage_metadata:
            usage = response.usage_metadata
            prompt_tokens = getattr(usage, 'prompt_token_count', 0) or 0
            completion_tokens = getattr(usage, 'candidates_token_count', 0) or 0

            self.total_prompt_tokens += prompt_tokens
            self.total_completion_tokens += completion_tokens
            self.total_cost += self._calculate_cost(model_name, prompt_tokens, completion_tokens)

        self.api_call_count += 1

    def _traced_call(
        self,
        trace_name: Optional[str],
        prompt: str,
        llm_fn,
        model_name: str,
        default_trace_name: str,
        temperature: Optional[float] = None
    ):
        if not braintrust_enabled():
            return llm_fn()

        name = trace_name or default_trace_name
        environment = os.getenv("ENVIRONMENT", "local")
        return get_braintrust_client().traced_call(
            name=name,
            input_data={"prompt": prompt},
            llm_call_fn=llm_fn,
            prompt=prompt,
            metadata={
                "model": model_name,
                "temperature": temperature if temperature is not None else self.default_temperature,
                "environment": environment
            }
        )

    def generate_structured_content(
        self,
        prompt: str,
        response_schema: Union[Type[BaseModel], Dict[str, Any]],
        model: Optional[GeminiModelType] = None,
        temperature: Optional[float] = None,
        thinking_level: Optional[ThinkingLevel] = None,
        system_instruction: Optional[str] = None,
        trace_name: Optional[str] = None
    ) -> Union[BaseModel, List[BaseModel], Dict[str, Any]]:
        effective_model = model or self.default_model
        model_name = effective_model.value
        config = self._build_config(effective_model, temperature, thinking_level)
        config.response_mime_type = "application/json"
        config.response_schema = response_schema

        if system_instruction:
            config.system_instruction = system_instruction

        is_pydantic = isinstance(response_schema, type) and issubclass(response_schema, BaseModel)

        def _execute_call():
            for attempt in range(self.max_retries):
                try:
                    response = self.client.models.generate_content(
                        model=model_name,
                        contents=prompt,
                        config=config
                    )
                    self._update_usage(model_name, response)

                    if response.text:
                        data = json.loads(response.text)
                        if is_pydantic:
                            if isinstance(data, list):
                                return [response_schema(**item) for item in data]
                            return response_schema(**data)
                        return data

                    raise ValueError("Empty response from API")

                except Exception as e:
                    if attempt < self.max_retries - 1:
                        delay = self.base_delay * (2 ** attempt)
                        self.logger.warning(f"Attempt {attempt + 1} failed: {e}. Retrying in {delay}s")
                        time.sleep(delay)
                    else:
                        self.logger.error(f"All {self.max_retries} attempts failed: {e}")
                        raise

        return self._traced_call(
            trace_name=trace_name,
            prompt=prompt,
            llm_fn=_execute_call,
            model_name=model_name,
            default_trace_name="generate_structured_content",
            temperature=temperature
        )

    def generate_content(
        self,
        prompt: str,
        model: Optional[GeminiModelType] = None,
        temperature: Optional[float] = None,
        thinking_level: Optional[ThinkingLevel] = None,
        system_instruction: Optional[str] = None,
        trace_name: Optional[str] = None
    ) -> str:
        effective_model = model or self.default_model
        model_name = effective_model.value
        config = self._build_config(effective_model, temperature, thinking_level)

        if system_instruction:
            config.system_instruction = system_instruction

        def _execute_call():
            for attempt in range(self.max_retries):
                try:
                    response = self.client.models.generate_content(
                        model=model_name,
                        contents=prompt,
                        config=config
                    )
                    self._update_usage(model_name, response)

                    if response.text:
                        return response.text

                    raise ValueError("Empty response from API")

                except Exception as e:
                    if attempt < self.max_retries - 1:
                        delay = self.base_delay * (2 ** attempt)
                        self.logger.warning(f"Attempt {attempt + 1} failed: {e}. Retrying in {delay}s")
                        time.sleep(delay)
                    else:
                        self.logger.error(f"All {self.max_retries} attempts failed: {e}")
                        raise

        return self._traced_call(
            trace_name=trace_name,
            prompt=prompt,
            llm_fn=_execute_call,
            model_name=model_name,
            default_trace_name="generate_content",
            temperature=temperature
        )

    def get_usage_stats(self) -> Dict[str, Any]:
        return {
            "api_calls": self.api_call_count,
            "prompt_tokens": self.total_prompt_tokens,
            "completion_tokens": self.total_completion_tokens,
            "total_cost": self.total_cost
        }


if __name__ == "__main__":
    print("=" * 60)
    print("Gemini 3 Client Integration Tests")
    print("=" * 60)

    client = Gemini3Client(thinking_level=ThinkingLevel.MINIMAL)

    print("\n1. Testing generate_content (simple text)...")
    try:
        result = client.generate_content(
            prompt="Say 'hello world' and nothing else."
        )
        print(f"   Response: {result}")
        assert "hello" in result.lower(), "Expected 'hello' in response"
        print("   PASSED")
    except Exception as e:
        print(f"   FAILED: {e}")

    print("\n2. Testing generate_content with different thinking levels...")
    for level in ThinkingLevel:
        try:
            test_client = Gemini3Client(thinking_level=level)
            result = test_client.generate_content(
                prompt=f"What is 2+2? Just say the number."
            )
            print(f"   {level.value}: {result.strip()}")
        except Exception as e:
            print(f"   {level.value}: FAILED - {e}")

    print("\n3. Testing generate_structured_content (JSON schema)...")

    class MathResponse(BaseModel):
        answer: int
        explanation: str

    try:
        result = client.generate_structured_content(
            prompt="What is 15 + 27? Provide the answer and a brief explanation.",
            response_schema=MathResponse
        )
        print(f"   Answer: {result.answer}")
        print(f"   Explanation: {result.explanation}")
        assert result.answer == 42, f"Expected 42, got {result.answer}"
        print("   PASSED")
    except Exception as e:
        print(f"   FAILED: {e}")

    print("\n4. Testing generate_structured_content (list response)...")

    class Item(BaseModel):
        name: str
        category: str

    try:
        result = client.generate_structured_content(
            prompt="List 3 fruits with their category. Return as a JSON array.",
            response_schema=Item
        )
        if isinstance(result, list):
            print(f"   Got {len(result)} items:")
            for item in result:
                print(f"      - {item.name} ({item.category})")
            print("   PASSED")
        else:
            print(f"   Got single item: {result}")
            print("   PASSED (single item)")
    except Exception as e:
        print(f"   FAILED: {e}")

    print("\n5. Testing system instruction...")
    try:
        result = client.generate_content(
            prompt="What is your name?",
            system_instruction="You are a helpful assistant named Bob. Always mention your name in responses."
        )
        print(f"   Response: {result}")
        assert "bob" in result.lower(), "Expected 'Bob' in response"
        print("   PASSED")
    except Exception as e:
        print(f"   FAILED: {e}")

    print("\n6. Testing PRO_3 model (note: PRO_3 doesn't support MINIMAL, using LOW)...")
    try:
        pro_client = Gemini3Client(
            default_model=GeminiModelType.PRO_3,
            thinking_level=ThinkingLevel.LOW
        )
        result = pro_client.generate_content(
            prompt="What is the capital of France? One word answer."
        )
        print(f"   Response: {result}")
        assert "paris" in result.lower(), "Expected 'Paris' in response"
        print("   PASSED")
    except Exception as e:
        print(f"   FAILED: {e}")

    print("\n" + "=" * 60)
    print("Usage Statistics")
    print("=" * 60)
    stats = client.get_usage_stats()
    print(f"   API calls: {stats['api_calls']}")
    print(f"   Prompt tokens: {stats['prompt_tokens']}")
    print(f"   Completion tokens: {stats['completion_tokens']}")
    print(f"   Total cost: ${stats['total_cost']:.6f}")
    print("\nIntegration tests complete!")
