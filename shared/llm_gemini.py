import json
import os
from collections.abc import AsyncIterator
from enum import Enum
from typing import Any

from dotenv import load_dotenv
from google import genai
from google.genai import types
from PIL import Image
from pydantic import BaseModel

from shared.logger import get_logger

load_dotenv()


"""
Google Gemini Thinking Capabilities by Model:

┌─────────────────┬──────────────────┬─────────────────┬────────────────┐
│ Model           │ Default          │ Budget Range    │ Disable?       │
├─────────────────┼──────────────────┼─────────────────┼────────────────┤
│ 2.5 Pro         │ Dynamic (-1)     │ 128 - 32768     │ No             │
│ 2.5 Flash       │ Dynamic (-1)     │ 0 - 24576       │ Yes (budget=0) │
│ 2.5 Flash Lite  │ No thinking      │ 512 - 24576     │ Yes (budget=0) │
│ 2.0 Flash       │ Dynamic (-1)     │ 0 - 24576       │ Yes (budget=0) │
│ 2.0 Flash Lite  │ No thinking      │ 512 - 24576     │ Yes (budget=0) │
└─────────────────┴──────────────────┴─────────────────┴────────────────┘

Token Tracking Features:
- total_tokens: All tokens used in the request/response
- total_thinking_tokens: Tokens used for internal reasoning
- total_search_tokens: Tokens used for search/grounding operations (tool_use_prompt_token_count)

Usage Examples:
- thinking_budget=0: Disable thinking (Flash models only)
- thinking_budget=-1: Dynamic thinking (model decides)
- thinking_budget=1024: Fixed thinking budget
- include_thoughts=True: Include thought summaries in response
"""


class GeminiModelType(Enum):
    FLASH = "gemini-2.5-flash"
    PRO = "gemini-2.5-pro"
    FLASH_LITE = "gemini-2.5-flash-lite"
    FLASH_2_0 = "gemini-2.0-flash"
    FLASH_2_0_LITE = "gemini-2.0-flash-lite"


class ContentType(Enum):
    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"


class GeminiClient:
    def __init__(
        self,
        api_key: str | None = None,
        default_model: GeminiModelType = GeminiModelType.FLASH,
        default_temperature: float = 0.7,
        default_max_tokens: int = 10000,
        thinking_budget: int | None = None,
        include_thoughts: bool = False
    ):
        self.api_key = api_key or os.getenv("GEMINI_API_KEY")
        if not self.api_key:
            raise ValueError("Google API key is required")

        self.client = genai.Client(api_key=self.api_key)
        self.default_model = default_model
        self.default_temperature = default_temperature
        self.default_max_tokens = default_max_tokens
        self.thinking_budget = thinking_budget
        self.include_thoughts = include_thoughts

        self.logger = get_logger(__name__)

        self.total_tokens = 0
        self.total_thinking_tokens = 0
        self.total_search_tokens = 0
        self.api_call_count = 0

        self.logger.info(f"Gemini client initialized with model: {default_model.value}")

    def _get_base_config(
        self,
        temperature: float | None = None,
        max_tokens: int | None = None,
        thinking_budget: int | None = None,
        include_thoughts: bool | None = None
    ) -> types.GenerateContentConfig:
        config_params = {
            "temperature": temperature or self.default_temperature,
            "max_output_tokens": max_tokens or self.default_max_tokens
        }

        thinking_config_params = {}

        budget = thinking_budget if thinking_budget is not None else self.thinking_budget

        if budget is not None:
            if self.default_model == GeminiModelType.PRO:
                if budget == 0:
                    self.logger.warning("Cannot disable thinking for Gemini 2.5 Pro - using minimum budget of 128")
                    budget = 128
                elif budget < 128 or budget > 32768:
                    self.logger.warning(f"Invalid budget {budget} for Pro model. Using valid range 128-32768")
                    budget = max(128, min(32768, budget))
            elif self.default_model == GeminiModelType.FLASH:
                if budget < 0 or budget > 24576:
                    self.logger.warning(f"Invalid budget {budget} for Flash model. Using valid range 0-24576")
                    budget = max(0, min(24576, budget))
            elif self.default_model == GeminiModelType.FLASH_LITE:
                if budget < 512 or budget > 24576:
                    self.logger.warning(f"Invalid budget {budget} for Flash Lite model. Using valid range 512-24576")
                    budget = max(512, min(24576, budget))

            thinking_config_params["thinking_budget"] = budget

        if include_thoughts is not None:
            thinking_config_params["include_thoughts"] = include_thoughts
        elif self.include_thoughts:
            thinking_config_params["include_thoughts"] = self.include_thoughts

        if thinking_config_params:
            config_params["thinking_config"] = types.ThinkingConfig(**thinking_config_params)

        return types.GenerateContentConfig(**config_params)

    def _track_usage(self, response):
        if hasattr(response, 'usage_metadata') and response.usage_metadata:
            usage = response.usage_metadata
            total_tokens = getattr(usage, 'total_token_count', 0) or 0
            thinking_tokens = getattr(usage, 'thoughts_token_count', 0) or 0
            search_tokens = getattr(usage, 'tool_use_prompt_token_count', 0) or 0

            self.total_tokens += total_tokens
            self.total_thinking_tokens += thinking_tokens
            self.total_search_tokens += search_tokens
            self.api_call_count += 1

            self.logger.debug(f"Token usage - Total: {total_tokens}, Thinking: {thinking_tokens}, Search: {search_tokens}")
            self.logger.debug(f"Session totals - Tokens: {self.total_tokens}, Thinking: {self.total_thinking_tokens}, Search: {self.total_search_tokens}, Calls: {self.api_call_count}")

    def get_usage_stats(self) -> dict[str, int]:
        return {
            "total_tokens": self.total_tokens,
            "total_thinking_tokens": self.total_thinking_tokens,
            "total_search_tokens": self.total_search_tokens,
            "api_call_count": self.api_call_count,
            "average_tokens_per_call": self.total_tokens // max(1, self.api_call_count),
            "average_thinking_tokens_per_call": self.total_thinking_tokens // max(1, self.api_call_count),
            "average_search_tokens_per_call": self.total_search_tokens // max(1, self.api_call_count)
        }

    def reset_usage_stats(self) -> dict[str, int]:
        previous_stats = self.get_usage_stats()
        self.total_tokens = 0
        self.total_thinking_tokens = 0
        self.total_search_tokens = 0
        self.api_call_count = 0
        return previous_stats

    def generate_content(
        self,
        prompt: str,
        model: GeminiModelType | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        system_instruction: str | None = None,
        thinking_budget: int | None = None,
        include_thoughts: bool | None = None
    ) -> str:
        model_name = (model or self.default_model).value
        config = self._get_base_config(temperature, max_tokens, thinking_budget, include_thoughts)

        if system_instruction:
            config.system_instruction = system_instruction

        try:
            response = self.client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=config
            )

            self._track_usage(response)
            return response.text

        except Exception as e:
            self.logger.error(f"Content generation failed: {e!s}")
            raise

    def generate_content_stream(
        self,
        prompt: str,
        model: GeminiModelType | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        system_instruction: str | None = None,
        thinking_budget: int | None = None,
        include_thoughts: bool | None = None
    ) -> AsyncIterator[str]:
        model_name = (model or self.default_model).value
        config = self._get_base_config(temperature, max_tokens, thinking_budget, include_thoughts)

        if system_instruction:
            config.system_instruction = system_instruction

        try:
            response = self.client.models.generate_content_stream(
                model=model_name,
                contents=prompt,
                config=config
            )

            for chunk in response:
                yield chunk.text

        except Exception as e:
            self.logger.error(f"Streaming generation failed: {e!s}")
            raise

    def generate_structured_content(
        self,
        prompt: str,
        response_schema: type[BaseModel] | list[type[BaseModel]] | dict[str, Any],
        model: GeminiModelType | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        system_instruction: str | None = None,
        thinking_budget: int | None = None,
        include_thoughts: bool | None = None
    ) -> BaseModel | list[BaseModel] | dict[str, Any]:
        model_name = (model or self.default_model).value
        config = self._get_base_config(temperature, max_tokens, thinking_budget, include_thoughts)

        if system_instruction:
            config.system_instruction = system_instruction

        config.response_mime_type = "application/json"
        config.response_schema = response_schema

        try:
            response = self.client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=config
            )

            self._track_usage(response)

            if hasattr(response, 'parsed') and response.parsed:
                return response.parsed

            return json.loads(response.text)

        except Exception as e:
            self.logger.error(f"Structured content generation failed: {e!s}")
            raise

    def generate_enum_content(
        self,
        prompt: str,
        enum_options: list[str],
        model: GeminiModelType | None = None,
        temperature: float | None = None,
        system_instruction: str | None = None,
        thinking_budget: int | None = None,
        include_thoughts: bool | None = None
    ) -> str:
        model_name = (model or self.default_model).value
        config = self._get_base_config(temperature, None, thinking_budget, include_thoughts)

        if system_instruction:
            config.system_instruction = system_instruction

        config.response_mime_type = "text/x.enum"
        config.response_schema = {
            "type": "STRING",
            "enum": enum_options
        }

        try:
            response = self.client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=config
            )

            self._track_usage(response)
            return response.text

        except Exception as e:
            self.logger.error(f"Enum content generation failed: {e!s}")
            raise

    def generate_with_search(
        self,
        prompt: str,
        model: GeminiModelType | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        system_instruction: str | None = None,
        thinking_budget: int | None = None,
        include_thoughts: bool | None = None
    ) -> dict[str, Any]:
        model_name = (model or self.default_model).value
        config = self._get_base_config(temperature, max_tokens, thinking_budget, include_thoughts)

        if system_instruction:
            config.system_instruction = system_instruction

        grounding_tool = types.Tool(google_search=types.GoogleSearch())
        config.tools = [grounding_tool]

        try:
            response = self.client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=config
            )

            self._track_usage(response)

            result = {
                "text": response.text,
                "grounding_metadata": None,
                "search_queries": [],
                "sources": []
            }

            if hasattr(response, 'candidates') and response.candidates:
                candidate = response.candidates[0]
                if hasattr(candidate, 'grounding_metadata') and candidate.grounding_metadata:
                    metadata = candidate.grounding_metadata
                    result["grounding_metadata"] = metadata

                    if hasattr(metadata, 'web_search_queries'):
                        result["search_queries"] = metadata.web_search_queries

                    if hasattr(metadata, 'grounding_chunks'):
                        result["sources"] = [
                            {
                                "title": chunk.web.title if hasattr(chunk, 'web') else "Unknown",
                                "uri": chunk.web.uri if hasattr(chunk, 'web') else "Unknown"
                            }
                            for chunk in metadata.grounding_chunks
                        ]

            return result

        except Exception as e:
            self.logger.error(f"Search-grounded generation failed: {e!s}")
            raise

    def generate_multimodal_content(
        self,
        prompt: str,
        media_path: str,
        content_type: ContentType,
        model: GeminiModelType | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        system_instruction: str | None = None,
        thinking_budget: int | None = None,
        include_thoughts: bool | None = None
    ) -> str:
        model_name = (model or self.default_model).value
        config = self._get_base_config(temperature, max_tokens, thinking_budget, include_thoughts)

        if system_instruction:
            config.system_instruction = system_instruction

        try:
            contents = []

            if content_type == ContentType.IMAGE:
                image = Image.open(media_path)
                contents = [image, prompt]
            else:
                with open(media_path, 'rb') as f:
                    media_data = f.read()
                contents = [media_data, prompt]

            response = self.client.models.generate_content(
                model=model_name,
                contents=contents,
                config=config
            )

            self._track_usage(response)
            return response.text

        except Exception as e:
            self.logger.error(f"Multimodal content generation failed: {e!s}")
            raise

    def generate_with_thoughts(
        self,
        prompt: str,
        model: GeminiModelType | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        system_instruction: str | None = None,
        thinking_budget: int | None = None
    ) -> dict[str, Any]:
        model_name = (model or self.default_model).value
        config = self._get_base_config(temperature, max_tokens, thinking_budget, True)

        if system_instruction:
            config.system_instruction = system_instruction

        try:
            response = self.client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=config
            )

            self._track_usage(response)

            result = {
                "text": "",
                "thoughts": "",
                "raw_response": response
            }

            if hasattr(response, 'candidates') and response.candidates:
                candidate = response.candidates[0]
                if hasattr(candidate, 'content') and candidate.content:
                    for part in candidate.content.parts:
                        if hasattr(part, 'text') and part.text:
                            if hasattr(part, 'thought') and part.thought:
                                result["thoughts"] = part.text
                            else:
                                result["text"] = part.text

            return result

        except Exception as e:
            self.logger.error(f"Thought generation failed: {e!s}")
            raise

    def extract_thoughts_from_response(self, response) -> dict[str, str]:
        thoughts = ""
        text = ""

        if hasattr(response, 'candidates') and response.candidates:
            candidate = response.candidates[0]
            if hasattr(candidate, 'content') and candidate.content:
                for part in candidate.content.parts:
                    if hasattr(part, 'text') and part.text:
                        if hasattr(part, 'thought') and part.thought:
                            thoughts += part.text
                        else:
                            text += part.text

        return {"thoughts": thoughts, "text": text}

    def get_thinking_token_count(self, response) -> int:
        if hasattr(response, 'usage_metadata') and response.usage_metadata:
            return getattr(response.usage_metadata, 'thoughts_token_count', 0) or 0
        return 0

    def get_search_token_count(self, response) -> int:
        if hasattr(response, 'usage_metadata') and response.usage_metadata:
            return getattr(response.usage_metadata, 'tool_use_prompt_token_count', 0) or 0
        return 0

    def extract_thought_signatures(self, response) -> list[str]:
        signatures = []
        if hasattr(response, 'candidates') and response.candidates:
            candidate = response.candidates[0]
            if hasattr(candidate, 'content') and candidate.content:
                for part in candidate.content.parts:
                    if hasattr(part, 'thought_signature') and part.thought_signature:
                        signatures.append(part.thought_signature)
        return signatures

    def disable_thinking(self):
        if self.default_model == GeminiModelType.PRO:
            self.logger.warning("Cannot disable thinking for Gemini 2.5 Pro model - it only supports dynamic thinking")
            return
        self.thinking_budget = 0
        self.logger.info("Thinking disabled for this client")

    def enable_dynamic_thinking(self):
        self.thinking_budget = -1
        self.logger.info("Dynamic thinking enabled for this client")

    def set_thinking_budget(self, budget: int):
        model_name = self.default_model.value

        if self.default_model == GeminiModelType.PRO:
            if budget == 0:
                self.logger.warning("Cannot disable thinking for Gemini 2.5 Pro - using minimum budget of 128")
                budget = 128
            elif budget < 128 or budget > 32768:
                self.logger.warning(f"Invalid budget {budget} for Pro model. Using valid range 128-32768")
                budget = max(128, min(32768, budget))
        elif self.default_model == GeminiModelType.FLASH:
            if budget < 0 or budget > 24576:
                self.logger.warning(f"Invalid budget {budget} for Flash model. Using valid range 0-24576")
                budget = max(0, min(24576, budget))
        elif self.default_model == GeminiModelType.FLASH_LITE:
            if budget < 512 or budget > 24576:
                self.logger.warning(f"Invalid budget {budget} for Flash Lite model. Using valid range 512-24576")
                budget = max(512, min(24576, budget))

        self.thinking_budget = budget
        self.logger.info(f"Thinking budget set to {budget} tokens for {model_name}")


class GeminiChatClient:
    def __init__(
        self,
        api_key: str | None = None,
        default_model: GeminiModelType = GeminiModelType.FLASH,
        default_temperature: float = 0.7,
        default_max_tokens: int = 10000,
        system_instruction: str | None = None
    ):
        self.api_key = api_key or os.getenv("GEMINI_API_KEY")
        if not self.api_key:
            raise ValueError("Google API key is required")

        self.client = genai.Client(api_key=self.api_key)
        self.default_model = default_model
        self.default_temperature = default_temperature
        self.default_max_tokens = default_max_tokens
        self.system_instruction = system_instruction

        self.logger = get_logger(__name__)

        self.total_tokens = 0
        self.total_search_tokens = 0
        self.api_call_count = 0

        self.chat = None
        self._initialize_chat()

    def _initialize_chat(self):
        config = types.GenerateContentConfig(
            temperature=self.default_temperature,
            max_output_tokens=self.default_max_tokens
        )

        if self.system_instruction:
            config.system_instruction = self.system_instruction

        self.chat = self.client.chats.create(
            model=self.default_model.value,
            config=config
        )

    def _track_usage(self, response):
        if hasattr(response, 'usage_metadata') and response.usage_metadata:
            usage = response.usage_metadata
            total_tokens = getattr(usage, 'total_token_count', 0) or 0
            search_tokens = getattr(usage, 'tool_use_prompt_token_count', 0) or 0

            self.total_tokens += total_tokens
            self.total_search_tokens += search_tokens
            self.api_call_count += 1

            self.logger.debug(f"Token usage - Total: {total_tokens}, Search: {search_tokens}")
            self.logger.debug(f"Session totals - Tokens: {self.total_tokens}, Search: {self.total_search_tokens}, Calls: {self.api_call_count}")

    def send_message(self, message: str) -> str:
        if not self.chat:
            self._initialize_chat()

        try:
            response = self.chat.send_message(message)
            self._track_usage(response)
            return response.text

        except Exception as e:
            self.logger.error(f"Chat message failed: {e!s}")
            raise

    def send_message_stream(self, message: str) -> AsyncIterator[str]:
        if not self.chat:
            self._initialize_chat()

        try:
            response = self.chat.send_message_stream(message)

            for chunk in response:
                yield chunk.text

        except Exception as e:
            self.logger.error(f"Chat streaming failed: {e!s}")
            raise

    def get_history(self) -> list[dict[str, str]]:
        if not self.chat:
            return []

        try:
            history = []
            for message in self.chat.get_history():
                history.append({
                    "role": message.role,
                    "content": message.parts[0].text if message.parts else ""
                })
            return history

        except Exception as e:
            self.logger.error(f"Failed to get chat history: {e!s}")
            return []

    def clear_history(self):
        self._initialize_chat()
        self.logger.info("Chat history cleared")

    def get_usage_stats(self) -> dict[str, int]:
        return {
            "total_tokens": self.total_tokens,
            "total_search_tokens": self.total_search_tokens,
            "api_call_count": self.api_call_count,
            "average_tokens_per_call": self.total_tokens // max(1, self.api_call_count),
            "average_search_tokens_per_call": self.total_search_tokens // max(1, self.api_call_count)
        }

    def reset_usage_stats(self) -> dict[str, int]:
        previous_stats = self.get_usage_stats()
        self.total_tokens = 0
        self.total_search_tokens = 0
        self.api_call_count = 0
        return previous_stats


class GeminiFunctionClient:
    def __init__(
        self,
        api_key: str | None = None,
        default_model: GeminiModelType = GeminiModelType.FLASH,
        default_temperature: float = 0.7,
        auto_execute: bool = False
    ):
        self.api_key = api_key or os.getenv("GEMINI_API_KEY")
        if not self.api_key:
            raise ValueError("Google API key is required")

        self.client = genai.Client(api_key=self.api_key)
        self.default_model = default_model
        self.default_temperature = default_temperature
        self.auto_execute = auto_execute

        self.logger = get_logger(__name__)

        self.total_tokens = 0
        self.total_search_tokens = 0
        self.api_call_count = 0
        self.functions = {}

    def register_function(self, func: callable, declaration: dict[str, Any] | None = None):
        if declaration:
            self.functions[declaration["name"]] = func
        else:
            func_declaration = types.FunctionDeclaration.from_callable(
                callable=func,
                client=self.client
            )
            self.functions[func_declaration.name] = func

    def generate_with_functions(
        self,
        prompt: str,
        functions: list[callable],
        model: GeminiModelType | None = None,
        temperature: float | None = None,
        function_calling_mode: str = "AUTO"
    ) -> dict[str, Any]:
        model_name = (model or self.default_model).value

        config = types.GenerateContentConfig(
            temperature=temperature or self.default_temperature,
            tools=functions if self.auto_execute else [
                types.Tool(function_declarations=[
                    types.FunctionDeclaration.from_callable(callable=func, client=self.client)
                    for func in functions
                ])
            ]
        )

        if function_calling_mode != "AUTO":
            config.tool_config = types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(
                    mode=function_calling_mode
                )
            )

        try:
            response = self.client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=config
            )

            self._track_usage(response)

            result = {
                "text": response.text,
                "function_calls": [],
                "function_results": []
            }

            if hasattr(response, 'candidates') and response.candidates:
                candidate = response.candidates[0]
                if hasattr(candidate, 'content') and candidate.content:
                    for part in candidate.content.parts:
                        if hasattr(part, 'function_call') and part.function_call:
                            function_call = part.function_call
                            result["function_calls"].append({
                                "name": function_call.name,
                                "args": dict(function_call.args)
                            })

                            if not self.auto_execute and function_call.name in self.functions:
                                try:
                                    func_result = self.functions[function_call.name](**function_call.args)
                                    result["function_results"].append({
                                        "name": function_call.name,
                                        "result": func_result
                                    })
                                except Exception as e:
                                    self.logger.error(f"Function execution failed: {e!s}")
                                    result["function_results"].append({
                                        "name": function_call.name,
                                        "error": str(e)
                                    })

            return result

        except Exception as e:
            self.logger.error(f"Function-based generation failed: {e!s}")
            raise

    def _track_usage(self, response):
        if hasattr(response, 'usage_metadata') and response.usage_metadata:
            usage = response.usage_metadata
            total_tokens = getattr(usage, 'total_token_count', 0) or 0
            search_tokens = getattr(usage, 'tool_use_prompt_token_count', 0) or 0

            self.total_tokens += total_tokens
            self.total_search_tokens += search_tokens
            self.api_call_count += 1

            self.logger.debug(f"Token usage - Total: {total_tokens}, Search: {search_tokens}")
            self.logger.debug(f"Session totals - Tokens: {self.total_tokens}, Search: {self.total_search_tokens}, Calls: {self.api_call_count}")

    def get_usage_stats(self) -> dict[str, int]:
        return {
            "total_tokens": self.total_tokens,
            "total_search_tokens": self.total_search_tokens,
            "api_call_count": self.api_call_count,
            "average_tokens_per_call": self.total_tokens // max(1, self.api_call_count),
            "average_search_tokens_per_call": self.total_search_tokens // max(1, self.api_call_count)
        }

    def reset_usage_stats(self) -> dict[str, int]:
        previous_stats = self.get_usage_stats()
        self.total_tokens = 0
        self.total_search_tokens = 0
        self.api_call_count = 0
        return previous_stats


def example_usage():
    client = GeminiClient(
        default_model=GeminiModelType.PRO,
        thinking_budget=1024,
        include_thoughts=True
    )

    result = client.generate_with_thoughts(
        "What is the sum of the first 50 prime numbers? Show your reasoning step by step.",
        thinking_budget=2048
    )

    print("Model's thoughts:")
    print(result["thoughts"])
    print("\nFinal answer:")
    print(result["text"])

    usage = client.get_usage_stats()
    print(f"\nUsage: {usage['total_tokens']} total tokens, {usage['total_thinking_tokens']} thinking tokens, {usage['total_search_tokens']} search tokens")

    flash_client = GeminiClient(
        default_model=GeminiModelType.FLASH,
        thinking_budget=0,
        include_thoughts=False
    )

    simple_result = flash_client.generate_content(
        "What is 2+2?",
        thinking_budget=0
    )
    print(f"\nSimple answer (no thinking): {simple_result}")


def example_search_with_thinking():
    client = GeminiClient(
        default_model=GeminiModelType.FLASH,
        thinking_budget=512,
        include_thoughts=True
    )

    result = client.generate_with_search(
        "What are the latest developments in AI reasoning models in 2025?",
        thinking_budget=1024,
        include_thoughts=True
    )

    print("Search results with thinking:")
    print(result["text"])
    print(f"\nSources: {len(result['sources'])} found")

    for i, source in enumerate(result["sources"][:3]):
        print(f"{i+1}. {source['title']}: {source['uri']}")


def example_streaming_with_thinking():
    client = GeminiClient(
        default_model=GeminiModelType.FLASH,
        thinking_budget=1024,
        include_thoughts=True
    )

    prompt = "Alice, Bob, and Carol each live in a different house on the same street: red, green, and blue. The person who lives in the red house owns a cat. Bob does not live in the green house. Carol owns a dog. The green house is to the left of the red house. Alice does not own a cat. Who lives in each house, and what pet do they own?"

    print("Streaming response with thinking:")
    print("Note: This example demonstrates basic streaming. For thinking-aware streaming, use generate_with_thoughts() instead.")

    print("\nStreaming answer:")
    for chunk_text in client.generate_content_stream(
        prompt,
        thinking_budget=1024,
        include_thoughts=True
    ):
        print(chunk_text, end='', flush=True)

    print("\n\nFor detailed thoughts, here's the non-streaming version:")
    result = client.generate_with_thoughts(
        prompt,
        thinking_budget=1024
    )

    if result["thoughts"]:
        print("\nModel's thoughts:")
        print(result["thoughts"])

    print("\nFinal answer:")
    print(result["text"])


def example_model_thinking_constraints():
    print("=== Model-Specific Thinking Constraints ===\n")

    print("1. Gemini 2.5 Pro - Cannot disable thinking:")
    pro_client = GeminiClient(default_model=GeminiModelType.PRO)
    pro_client.disable_thinking()

    print("\n2. Gemini 2.5 Flash - Can disable thinking:")
    flash_client = GeminiClient(default_model=GeminiModelType.FLASH)
    flash_client.disable_thinking()

    result = flash_client.generate_content("What is 2+2?")
    print(f"Flash without thinking: {result}")

    print("\n3. Different thinking budgets:")

    pro_client.set_thinking_budget(512)
    flash_client.set_thinking_budget(512)

    print("\n4. Dynamic thinking (-1 budget):")
    pro_client.enable_dynamic_thinking()
    flash_client.enable_dynamic_thinking()

    print("\n5. Usage stats comparison:")
    pro_stats = pro_client.get_usage_stats()
    flash_stats = flash_client.get_usage_stats()
    print(f"Pro client stats: {pro_stats['total_tokens']} total, {pro_stats['total_thinking_tokens']} thinking, {pro_stats['total_search_tokens']} search")
    print(f"Flash client stats: {flash_stats['total_tokens']} total, {flash_stats['total_thinking_tokens']} thinking, {flash_stats['total_search_tokens']} search")


if __name__ == "__main__":
    example_usage()
    print("\n" + "="*50 + "\n")
    example_search_with_thinking()
    print("\n" + "="*50 + "\n")
    example_streaming_with_thinking()
    print("\n" + "="*50 + "\n")
    example_model_thinking_constraints()
