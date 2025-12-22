import os
import json
import time
import asyncio
from typing import Optional, Dict, Any, List, Union, Type, Iterator
from enum import Enum
from pydantic import BaseModel
from dotenv import load_dotenv
import numpy as np
import httpx
from tqdm.asyncio import tqdm as atqdm
from tqdm import tqdm
from google import genai
from google.genai import types
from PIL import Image
from shared.logger import get_logger
from shared.braintrust import is_enabled as braintrust_enabled, get_client as get_braintrust_client

load_dotenv()

GEMINI_PRICING = {
    'gemini-2.5-flash': {'input': 0.075, 'output': 0.30},
    'gemini-2.5-pro': {'input': 2.50, 'output': 7.50},
    'gemini-2.5-flash-lite': {'input': 0.0375, 'output': 0.15},
    'gemini-embedding-001': {'input': 0.15, 'output': 0.0},
}


"""
Google Gemini Thinking Capabilities by Model:

┌─────────────────┬──────────────────┬─────────────────┬────────────────┐
│ Model           │ Default          │ Budget Range    │ Disable?       │
├─────────────────┼──────────────────┼─────────────────┼────────────────┤
│ 2.5 Pro         │ Dynamic (-1)     │ 128 - 32768     │ No             │
│ 2.5 Flash       │ Dynamic (-1)     │ 0 - 24576       │ Yes (budget=0) │
│ 2.5 Flash Lite  │ No thinking      │ 512 - 24576     │ Yes (budget=0) │
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

class ContentType(Enum):
    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"

class GeminiClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        default_model: GeminiModelType = GeminiModelType.FLASH,
        default_temperature: float = 0.7,
        default_max_tokens: Optional[int] = None,
        thinking_budget: Optional[int] = None,
        include_thoughts: bool = False,
        max_connections: int = 100,
        max_keepalive_connections: int = 20,
        max_retries: int = 3,
        base_delay: float = 1.0
    ):
        self.api_key = api_key or os.getenv("GEMINI_API_KEY")
        if not self.api_key:
            raise ValueError("Google API key is required")

        # Configure custom HTTP limits for high concurrency
        self.max_connections = max_connections
        self.max_keepalive_connections = max_keepalive_connections

        # Retry configuration
        self.max_retries = max_retries
        self.base_delay = base_delay
        
        # Create HTTP options with custom connection limits
        from google.genai.types import HttpOptions
        
        # Use minimal HTTP options - only connection limits
        http_options = HttpOptions(
            clientArgs={
                "limits": httpx.Limits(
                    max_connections=max_connections,
                    max_keepalive_connections=max_keepalive_connections
                ),
                "timeout": httpx.Timeout(
                    connect=10.0,
                    read=120.0,
                    write=30.0,
                    pool=10.0
                )
            },
            asyncClientArgs={
                "limits": httpx.Limits(
                    max_connections=max_connections,
                    max_keepalive_connections=max_keepalive_connections
                ),
                "timeout": httpx.Timeout(
                    connect=10.0,
                    read=120.0,
                    write=30.0,
                    pool=10.0
                )
            }
        )
        
        # Initialize Gemini client with custom HTTP options
        self.client = genai.Client(api_key=self.api_key, http_options=http_options)
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
        
        # Cost tracking
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.total_cost = 0.0
        
        self.logger.info(f"Gemini client initialized with model: {default_model.value}, max_connections: {max_connections}, max_keepalive: {max_keepalive_connections}")
    
    def _get_base_config(
        self,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        thinking_budget: Optional[int] = None,
        include_thoughts: Optional[bool] = None
    ) -> types.GenerateContentConfig:
        config_params = {
            "temperature": temperature or self.default_temperature
        }
        
        # Only set max_output_tokens if a limit is specified
        max_tokens_limit = max_tokens or self.default_max_tokens
        if max_tokens_limit:
            config_params["max_output_tokens"] = max_tokens_limit
        
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
    
    def _calculate_cost(self, model_name: str, prompt_tokens: int, completion_tokens: int) -> float:
        """Calculate cost for API usage based on model and token counts."""
        model_key = model_name.lower()
        if model_key not in GEMINI_PRICING:
            # Default to flash pricing if model not found
            model_key = 'gemini-2.5-flash'
            
        pricing = GEMINI_PRICING[model_key]
        input_cost = (prompt_tokens / 1_000_000) * pricing['input']
        output_cost = (completion_tokens / 1_000_000) * pricing['output']
        total_cost = input_cost + output_cost
        
        self.logger.debug(f"Cost calculated for {model_name}: ${total_cost:.6f} (input: ${input_cost:.6f}, output: ${output_cost:.6f})")
        return total_cost
    
    def _track_usage(self, response, model_name: str = None):
        if hasattr(response, 'usage_metadata') and response.usage_metadata:
            usage = response.usage_metadata
            total_tokens = getattr(usage, 'total_token_count', 0) or 0
            thinking_tokens = getattr(usage, 'thoughts_token_count', 0) or 0
            search_tokens = getattr(usage, 'tool_use_prompt_token_count', 0) or 0
            
            # Extract prompt and completion tokens for cost calculation
            prompt_tokens = getattr(usage, 'prompt_token_count', 0) or 0
            completion_tokens = getattr(usage, 'candidates_token_count', 0) or 0
            
            # If we don't have detailed breakdown, estimate from total
            if prompt_tokens == 0 and completion_tokens == 0 and total_tokens > 0:
                # Estimate: roughly 70% prompt, 30% completion for typical interactions
                prompt_tokens = int(total_tokens * 0.7)
                completion_tokens = total_tokens - prompt_tokens
            
            self.total_tokens += total_tokens
            self.total_thinking_tokens += thinking_tokens
            self.total_search_tokens += search_tokens
            self.total_prompt_tokens += prompt_tokens
            self.total_completion_tokens += completion_tokens
            self.api_call_count += 1
            
            # Calculate and track cost
            if model_name:
                cost = self._calculate_cost(model_name, prompt_tokens, completion_tokens)
                self.total_cost += cost
                
                self.logger.debug(f"Token usage - Total: {total_tokens}, Prompt: {prompt_tokens}, Completion: {completion_tokens}, Cost: ${cost:.6f}")
            else:
                self.logger.debug(f"Token usage - Total: {total_tokens}, Thinking: {thinking_tokens}, Search: {search_tokens}")
            
            self.logger.debug(f"Session totals - Tokens: {self.total_tokens}, Cost: ${self.total_cost:.6f}, Calls: {self.api_call_count}")
    
    def get_usage_stats(self) -> Dict[str, int]:
        return {
            "total_tokens": self.total_tokens,
            "total_thinking_tokens": self.total_thinking_tokens,
            "total_search_tokens": self.total_search_tokens,
            "total_prompt_tokens": self.total_prompt_tokens,
            "total_completion_tokens": self.total_completion_tokens,
            "api_call_count": self.api_call_count,
            "total_cost": self.total_cost,
            "average_tokens_per_call": self.total_tokens // max(1, self.api_call_count),
            "average_thinking_tokens_per_call": self.total_thinking_tokens // max(1, self.api_call_count),
            "average_search_tokens_per_call": self.total_search_tokens // max(1, self.api_call_count),
            "average_cost_per_call": self.total_cost / max(1, self.api_call_count)
        }
    
    def reset_usage_stats(self) -> Dict[str, int]:
        previous_stats = self.get_usage_stats()
        self.total_tokens = 0
        self.total_thinking_tokens = 0
        self.total_search_tokens = 0
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.total_cost = 0.0
        self.api_call_count = 0
        return previous_stats

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
            input_data={"prompt": prompt[:2000] if len(prompt) > 2000 else prompt},
            llm_call_fn=llm_fn,
            prompt=prompt,
            metadata={
                "model": model_name,
                "temperature": temperature or self.default_temperature,
                "environment": environment
            }
        )

    def generate_content(
        self,
        prompt: str,
        model: Optional[GeminiModelType] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        system_instruction: Optional[str] = None,
        thinking_budget: Optional[int] = None,
        include_thoughts: Optional[bool] = None,
        trace_name: Optional[str] = None
    ) -> str:
        model_name = (model or self.default_model).value
        config = self._get_base_config(temperature, max_tokens, thinking_budget, include_thoughts)

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

                    if response is None:
                        self.logger.warning(f"LLM attempt {attempt + 1}/{self.max_retries} - Response is None!")
                        if attempt < self.max_retries - 1:
                            delay = self.base_delay * (2 ** attempt)
                            self.logger.warning(f"Retrying None response in {delay}s...")
                            time.sleep(delay)
                            continue
                        else:
                            self.logger.error(f"Response is None after {self.max_retries} attempts")
                            return None

                    response_text = response.text

                    if response_text is None:
                        self.logger.warning(f"LLM attempt {attempt + 1}/{self.max_retries} - response.text is None!")
                        if attempt < self.max_retries - 1:
                            delay = self.base_delay * (2 ** attempt)
                            self.logger.warning(f"Retrying None response.text in {delay}s...")
                            time.sleep(delay)
                            continue
                        else:
                            self.logger.error(f"response.text is None after {self.max_retries} attempts")
                            return None

                    self._track_usage(response, model_name)
                    self.logger.debug(f"LLM attempt {attempt + 1}/{self.max_retries} - Success! Returning response.")
                    return response_text

                except Exception as e:
                    self.logger.warning(f"LLM attempt {attempt + 1}/{self.max_retries} - Exception caught: {type(e).__name__}: {str(e)}")
                    if attempt < self.max_retries - 1:
                        delay = self.base_delay * (2 ** attempt)
                        self.logger.warning(f"Content generation failed (attempt {attempt + 1}/{self.max_retries}): {str(e)}. Retrying in {delay}s...")
                        time.sleep(delay)
                    else:
                        self.logger.error(f"Content generation failed after {self.max_retries} attempts: {str(e)}")
                        raise

        return self._traced_call(
            trace_name=trace_name,
            prompt=prompt,
            llm_fn=_execute_call,
            model_name=model_name,
            default_trace_name="generate_content",
            temperature=temperature
        )
        
    def generate_structured_content(
        self,
        prompt: str,
        response_schema: Union[Type[BaseModel], List[Type[BaseModel]], Dict[str, Any]],
        model: Optional[GeminiModelType] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        system_instruction: Optional[str] = None,
        thinking_budget: Optional[int] = None,
        include_thoughts: Optional[bool] = None,
        trace_name: Optional[str] = None
    ) -> Union[BaseModel, List[BaseModel], Dict[str, Any]]:
        model_name = (model or self.default_model).value
        config = self._get_base_config(temperature, max_tokens, thinking_budget, include_thoughts)

        if system_instruction:
            config.system_instruction = system_instruction

        config.response_mime_type = "application/json"
        config.response_schema = response_schema

        def _execute_call():
            for attempt in range(self.max_retries):
                try:
                    response = self.client.models.generate_content(
                        model=model_name,
                        contents=prompt,
                        config=config
                    )

                    self._track_usage(response, model_name)

                    if hasattr(response, 'parsed') and response.parsed:
                        return response.parsed

                    response_text = response.text.strip() if response.text else ""
                    if not response_text:
                        self.logger.error(f"Empty response from {model_name} for structured content generation")
                        raise RuntimeError(f"Empty response from {model_name} - no content generated")

                    try:
                        return json.loads(response_text)
                    except json.JSONDecodeError as json_error:
                        self.logger.error(f"Invalid JSON response from {model_name}: '{response_text[:200]}...' Error: {json_error}")
                        raise RuntimeError(f"Invalid JSON response from {model_name}: {json_error}")

                except Exception as e:
                    if attempt < self.max_retries - 1:
                        delay = self.base_delay * (2 ** attempt)
                        self.logger.warning(f"Structured content generation failed (attempt {attempt + 1}/{self.max_retries}): {str(e)}. Retrying in {delay}s...")
                        time.sleep(delay)
                    else:
                        self.logger.error(f"Structured content generation failed after {self.max_retries} attempts: {str(e)}")
                        raise

        return self._traced_call(
            trace_name=trace_name,
            prompt=prompt,
            llm_fn=_execute_call,
            model_name=model_name,
            default_trace_name="generate_structured_content",
            temperature=temperature
        )
    
    def generate_with_search(
        self,
        prompt: str,
        model: Optional[GeminiModelType] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        system_instruction: Optional[str] = None,
        thinking_budget: Optional[int] = None,
        include_thoughts: Optional[bool] = None,
        trace_name: Optional[str] = None
    ) -> Dict[str, Any]:
        model_name = (model or self.default_model).value
        config = self._get_base_config(temperature, max_tokens, thinking_budget, include_thoughts)

        if system_instruction:
            config.system_instruction = system_instruction

        grounding_tool = types.Tool(google_search=types.GoogleSearch())
        config.tools = [grounding_tool]

        def _execute_call():
            for attempt in range(self.max_retries):
                try:
                    response = self.client.models.generate_content(
                        model=model_name,
                        contents=prompt,
                        config=config
                    )

                    self._track_usage(response, model_name)

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
                    if attempt < self.max_retries - 1:
                        delay = self.base_delay * (2 ** attempt)
                        self.logger.warning(f"Search-grounded generation failed (attempt {attempt + 1}/{self.max_retries}): {str(e)}. Retrying in {delay}s...")
                        time.sleep(delay)
                    else:
                        self.logger.error(f"Search-grounded generation failed after {self.max_retries} attempts: {str(e)}")
                        raise

        return self._traced_call(
            trace_name=trace_name,
            prompt=prompt,
            llm_fn=_execute_call,
            model_name=model_name,
            default_trace_name="generate_with_search",
            temperature=temperature
        )
    
    def generate_multimodal_content(
        self,
        prompt: str,
        media_path: str,
        content_type: ContentType,
        model: Optional[GeminiModelType] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        system_instruction: Optional[str] = None,
        thinking_budget: Optional[int] = None,
        include_thoughts: Optional[bool] = None,
        trace_name: Optional[str] = None
    ) -> str:
        model_name = (model or self.default_model).value
        config = self._get_base_config(temperature, max_tokens, thinking_budget, include_thoughts)

        if system_instruction:
            config.system_instruction = system_instruction

        def _execute_call():
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

                self._track_usage(response, model_name)
                return response.text

            except Exception as e:
                self.logger.error(f"Multimodal content generation failed: {str(e)}")
                raise

        return self._traced_call(
            trace_name=trace_name,
            prompt=f"[{content_type.value}] {prompt}",
            llm_fn=_execute_call,
            model_name=model_name,
            default_trace_name="generate_multimodal_content",
            temperature=temperature
        )
    
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

class GeminiEmbeddingClient:
    """
    Specialized Gemini client for embedding generation with advanced rate limiting and parallel processing.
    
    This client is optimized specifically for Gemini's embedding API and handles:
    - Adaptive rate limiting for 429 errors
    - Parallel batch processing with configurable concurrency
    - Progress tracking with visual progress bars
    - Exponential backoff retry logic
    """
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        max_retries: int = 5,
        base_delay: float = 1.0
    ):
        """
        Initialize Gemini embedding client with API key rotation support.
        
        Args:
            api_key: Gemini API key (uses GEMINI_API_KEY env var if None)
            max_retries: Maximum retry attempts per batch
            base_delay: Base delay for exponential backoff (seconds)
        """
        self.api_key = api_key or os.getenv('GEMINI_API_KEY')
        
        if not self.api_key:
            raise ValueError("GEMINI_API_KEY not found in environment variables")
        
        # Initialize logger first
        self.logger = get_logger(__name__)
        self.max_retries = max_retries
        self.base_delay = base_delay
        
        # Cost tracking
        self.total_embeddings_created = 0
        self.total_input_tokens = 0
        self.total_cost = 0.0
        
        # Initialize sync Gemini client
        self.genai_client = genai.Client(api_key=self.api_key)
        
        self.logger.info("GeminiEmbeddingClient initialized")
    
    
    def _estimate_token_count(self, text: str) -> int:
        """Estimate token count for text - roughly 4 characters per token."""
        return max(1, len(text) // 4)
    
    def _track_embedding_cost(self, texts: List[str], model: str = "gemini-embedding-001"):
        """Track cost for embedding creation."""
        # total_chars = sum(len(text) for text in texts)  # Not currently used
        estimated_tokens = self._estimate_token_count(' '.join(texts))
        
        # Calculate cost using pricing
        pricing = GEMINI_PRICING.get(model, GEMINI_PRICING['gemini-embedding-001'])
        cost = (estimated_tokens / 1_000_000) * pricing['input']
        
        # Update totals
        self.total_embeddings_created += len(texts)
        self.total_input_tokens += estimated_tokens
        self.total_cost += cost
        
        self.logger.debug(f"Embedding cost: ${cost:.6f} for {len(texts)} texts ({estimated_tokens} tokens)")
        return cost
    
    def get_cost_stats(self) -> Dict[str, float]:
        """Get embedding cost statistics."""
        return {
            "total_embeddings_created": self.total_embeddings_created,
            "total_input_tokens": self.total_input_tokens,
            "total_cost": self.total_cost,
            "average_cost_per_embedding": self.total_cost / max(1, self.total_embeddings_created),
            "average_tokens_per_embedding": self.total_input_tokens / max(1, self.total_embeddings_created)
        }
    
    def reset_cost_stats(self) -> Dict[str, float]:
        """Reset cost statistics and return previous values."""
        previous_stats = self.get_cost_stats()
        self.total_embeddings_created = 0
        self.total_input_tokens = 0
        self.total_cost = 0.0
        return previous_stats
    
    def _create_single_embedding(
        self,
        text: str,
        model: str = "gemini-embedding-001"
    ) -> np.ndarray:
        """
        Create embedding for a single text with retry logic and API key rotation.
        
        Args:
            text: Text to embed
            model: Embedding model to use
            
        Returns:
            numpy array containing the embedding
        """
        for attempt in range(self.max_retries):
            try:
                result = self.genai_client.models.embed_content(
                    model=model,
                    contents=text
                )
                
                # Track cost
                self._track_embedding_cost([text], model)
                
                return np.array(result.embeddings[0].values)
                
            except Exception as e:
                self.logger.warning(f"Single embedding attempt {attempt + 1}/{self.max_retries} failed: {str(e)}")
                
                if attempt < self.max_retries - 1:
                    delay = self.base_delay * (2 ** attempt)
                    self.logger.debug(f"Retrying in {delay} seconds...")
                    time.sleep(delay)
                else:
                    raise RuntimeError(f"Failed to create single embedding after {self.max_retries} attempts: {str(e)}")
    
    
    async def _create_embeddings_parallel(
        self,
        texts: List[str],
        model: str = "gemini-embedding-001",
        batch_size: int = 100,
        max_concurrent_batches: int = 2,
        rate_limit_delay: float = 2.0,
        adaptive_rate_limiting: bool = True,
        stagger_delay: float = 0.1
    ) -> np.ndarray:
        """
        Create embeddings using parallel batch processing with adaptive rate limiting.
        
        Args:
            texts: List of texts to embed
            model: Embedding model to use
            batch_size: Number of texts to process per batch
            max_concurrent_batches: Maximum number of concurrent batches
            rate_limit_delay: Base delay between batches (seconds)
            adaptive_rate_limiting: Whether to adapt delays based on 429 errors
            stagger_delay: Delay to stagger batch start times (seconds)
            
        Returns:
            numpy array of embeddings
        """
        self.logger.info(f"Creating embeddings for {len(texts)} texts using {model} (parallel)")
        
        # Split texts into batches
        batches = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            batches.append((i // batch_size, batch))
        
        total_batches = len(batches)
        self.logger.info(f"Processing {total_batches} batches with max {max_concurrent_batches} concurrent")
        
        # Adaptive rate limiting state
        current_delay = rate_limit_delay
        consecutive_429s = 0
        
        # Process batches in parallel with concurrency limit and progress bar
        semaphore = asyncio.Semaphore(max_concurrent_batches)
        progress_bar = atqdm(total=total_batches, desc="Processing batches", unit="batch")
        
        async def process_batch_with_retry(batch_num: int, batch_texts: List[str]) -> tuple:
            """Process a single batch with adaptive retry logic"""
            nonlocal current_delay, consecutive_429s
            
            async with semaphore:
                last_exception = None
                
                # Add staggered start delay for each batch
                stagger_wait = batch_num * stagger_delay
                if stagger_wait > 0:
                    await asyncio.sleep(stagger_wait)
                
                # Add rate limiting delay before processing
                if batch_num > 0:  # Don't delay the first batch
                    await asyncio.sleep(current_delay)
                
                for attempt in range(self.max_retries):
                    try:
                        async with httpx.AsyncClient(timeout=60.0) as client:
                            batch_embeddings = []
                            
                            for text in batch_texts:
                                # Use Gemini REST API directly for async calls
                                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:embedContent"
                                headers = {
                                    "Content-Type": "application/json",
                                    "x-goog-api-key": self.api_key
                                }
                                data = {
                                    "content": {"parts": [{"text": text}]},
                                    "taskType": "RETRIEVAL_DOCUMENT"
                                }
                                
                                response = await client.post(url, json=data, headers=headers)
                                response.raise_for_status()
                                
                                result = response.json()
                                embedding = result["embedding"]["values"]
                                batch_embeddings.append(embedding)
                            
                            # Reset consecutive 429s on success
                            if adaptive_rate_limiting and consecutive_429s > 0:
                                consecutive_429s = 0
                                current_delay = max(rate_limit_delay, current_delay * 0.8)  # Gradually reduce delay
                                self.logger.debug(f"Reduced rate limit delay to {current_delay:.2f}s after success")
                            
                            progress_bar.update(1)
                            self.logger.debug(f"Batch {batch_num + 1} completed successfully ({len(batch_embeddings)} embeddings)")
                            return (batch_num, batch_embeddings, batch_texts)  # Include texts for cost tracking
                            
                    except httpx.HTTPStatusError as e:
                        last_exception = e
                        
                        
                        # For all other HTTP errors, retry with exponential backoff
                        # Special handling for 429 errors with adaptive rate limiting
                        if e.response.status_code == 429:
                            consecutive_429s += 1
                            
                            if adaptive_rate_limiting:
                                # Exponentially increase delay for 429 errors
                                current_delay = min(current_delay * 2, 30.0)  # Cap at 30 seconds
                                self.logger.warning(f"429 Rate limit hit (consecutive: {consecutive_429s}). Increasing delay to {current_delay:.2f}s")
                        
                        
                        # Exponential backoff for all retryable errors
                        retry_delay = current_delay * (2 ** attempt)
                        self.logger.warning(f"Batch {batch_num + 1} attempt {attempt + 1}/{self.max_retries} HTTP {e.response.status_code} error. Retrying in {retry_delay:.1f}s")
                        
                        if attempt < self.max_retries - 1:
                            await asyncio.sleep(retry_delay)
                            continue
                        else:
                            progress_bar.update(1)  # Update progress even on failure
                            raise RuntimeError(f"Failed to create embeddings for batch {batch_num + 1} after {self.max_retries} attempts. Last error: {str(last_exception)}")
                    
                    except Exception as e:
                        last_exception = e
                        self.logger.warning(f"Batch {batch_num + 1} attempt {attempt + 1}/{self.max_retries} failed: {str(e)}")
                        
                        if attempt < self.max_retries - 1:
                            delay = self.base_delay * (2 ** attempt)
                            await asyncio.sleep(delay)
                        else:
                            progress_bar.update(1)  # Update progress even on failure
                            raise RuntimeError(f"Failed to create embeddings for batch {batch_num + 1} after {self.max_retries} attempts. Last error: {str(last_exception)}")
        
        # Execute all batches concurrently
        tasks = [process_batch_with_retry(batch_num, batch_texts) for batch_num, batch_texts in batches]
        
        try:
            results = await asyncio.gather(*tasks)
            progress_bar.close()
            
            # Sort results by batch number and flatten
            results.sort(key=lambda x: x[0])
            all_embeddings = []
            for _, batch_embeddings, batch_texts in results:
                all_embeddings.extend(batch_embeddings)
                # Track cost for this batch
                self._track_embedding_cost(batch_texts, model)
            
            self.logger.info(f"Successfully created {len(all_embeddings)} embeddings using parallel processing")
            return np.array(all_embeddings)
            
        except Exception as e:
            progress_bar.close()
            self.logger.error(f"Parallel embedding generation failed: {str(e)}")
            raise RuntimeError(f"Parallel embedding generation failed: {str(e)}")
    
    def create_embeddings(
        self,
        texts: List[str],
        parallel: bool = True,
        batch_size: int = 100,
        max_concurrent_batches: int = 2,
        rate_limit_delay: float = 2.0,
        stagger_delay: float = 0.1,
        **kwargs
    ) -> np.ndarray:
        """
        Create embeddings with automatic parallel/sync selection and rate limiting.
        
        Args:
            texts: List of texts to embed
            parallel: Whether to use parallel processing
            batch_size: Number of texts per batch
            max_concurrent_batches: Max concurrent batches (lower = fewer 429s)
            rate_limit_delay: Base delay between batches in seconds
            stagger_delay: Delay to stagger batch start times (seconds)
            **kwargs: Additional arguments
            
        Returns:
            numpy array of embeddings
        """
        # For single texts, always use single embedding
        if len(texts) == 1:
            return self._create_single_embedding(texts[0], **kwargs).reshape(1, -1)
        
        # For multiple texts, always use parallel processing (more efficient)
        return asyncio.run(self._create_embeddings_parallel(
            texts,
            batch_size=batch_size,
            max_concurrent_batches=max_concurrent_batches,
            rate_limit_delay=rate_limit_delay,
            stagger_delay=stagger_delay,
            **kwargs
        ))

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
