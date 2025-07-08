from datetime import date
import os
import json
import time
import re
from typing import Optional, Dict, Any, List
import openai
from dotenv import load_dotenv
from pydantic import BaseModel

from shared.logger import get_logger

load_dotenv()


class ReflectionResponse(BaseModel):
    """Pydantic model for reflection evaluation response."""
    criteria_adherence_score: int
    meets_criteria: bool
    issues_found: List[str]
    improvement_suggestions: List[str]


class LLMClient:
    """
    A client class that abstracts LLM interactions using OpenAI standards with built-in retry logic and provider fallback.
    """
    
    def __init__(
        self,
        providers: Optional[List[Dict[str, Any]]] = None,
        max_retries: int = 3,
        base_delay: float = 5.0,
        fallback_on_provider_failure: bool = True
    ):
        """
        Initialize the LLM client with multiple providers and fallback capability.
        
        Args:
            providers: List of provider configurations. If None, uses default Gemini + TogetherAI setup
            max_retries: Maximum number of retry attempts per provider
            base_delay: Base delay for exponential backoff (seconds)
            fallback_on_provider_failure: Whether to fallback to next provider on failure
        """
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.fallback_on_provider_failure = fallback_on_provider_failure
        self.logger = get_logger(__name__)
        
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.total_tokens = 0
        self.api_call_count = 0
        
        # Setup providers
        if providers is None:
            self.providers = self._get_default_providers()
        else:
            self.providers = providers
            
        # Initialize clients for each provider
        self.clients = []
        for provider in self.providers:
            try:
                if not provider.get("api_key"):
                    self.logger.warning(f"No API key found for provider {provider['name']}, skipping")
                    continue
                    
                client = openai.OpenAI(
                    api_key=provider["api_key"],
                    base_url=provider["base_url"]
                )
                self.clients.append({
                    "client": client,
                    "name": provider["name"],
                    "default_model": provider["default_model"],
                    "config": provider
                })
                self.logger.info(f"Initialized provider: {provider['name']}")
            except Exception as e:
                self.logger.warning(f"Failed to initialize provider {provider['name']}: {str(e)}")
        
        if not self.clients:
            raise ValueError("No valid providers configured. Please check your API keys and configurations.")
            
        self.logger.info(f"LLMClient initialized with {len(self.clients)} providers")
    
    def _get_default_providers(self) -> List[Dict[str, Any]]:
        """Get default provider configurations for Gemini + TogetherAI fallback."""
        return [
            {
                "name": "gemini",
                "api_key": os.getenv("GEMINI_API_KEY"),
                "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
                "default_model": "gemini-2.5-flash",
                "priority": 1
            },
            {
                "name": "together",
                "api_key": os.getenv("TOGETHER_API_KEY"),
                "base_url": "https://api.together.xyz/v1",
                "default_model": "deepseek-ai/DeepSeek-V3",
                "priority": 2
            }
        ]
    
    def get_current_provider_info(self) -> Dict[str, Any]:
        """Get information about available providers."""
        return {
            "total_providers": len(self.clients),
            "providers": [
                {
                    "name": client["name"],
                    "default_model": client["default_model"],
                    "available": True
                }
                for client in self.clients
            ]
        }
    
    def clean_response_content(self, content: str) -> str:
        """
        Clean LLM response content by removing think tags and other unwanted patterns.
        
        Args:
            content: Raw content from LLM response
            
        Returns:
            str: Cleaned content
        """
        if content is None:
            self.logger.warning("Received None content for cleaning, returning empty string")
            return ""
        
        self.logger.debug(f"Cleaning response content, original length: {len(content)}")
        cleaned_content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
        if len(cleaned_content) != len(content):
            self.logger.debug(f"Content cleaned, new length: {len(cleaned_content)}")
        return cleaned_content
    
    def _track_token_usage(self, result) -> None:
        """
        Track token usage from API response and update running totals.
        
        Args:
            result: API response object from AI vendor
        """
        if hasattr(result, 'usage') and result.usage:
            usage = result.usage
            prompt_tokens = getattr(usage, 'prompt_tokens', 0)
            completion_tokens = getattr(usage, 'completion_tokens', 0)
            total_tokens = getattr(usage, 'total_tokens', 0)
            
            self.total_prompt_tokens += prompt_tokens
            self.total_completion_tokens += completion_tokens
            self.total_tokens += total_tokens
            self.api_call_count += 1
            
            self.logger.debug(f"Token usage - Prompt: {prompt_tokens}, Completion: {completion_tokens}, Total: {total_tokens}")
            self.logger.debug(f"Session totals - Prompt: {self.total_prompt_tokens}, Completion: {self.total_completion_tokens}, Total: {self.total_tokens}, Calls: {self.api_call_count}")
        else:
            self.logger.debug("No usage information available in API response")
            self.api_call_count += 1
    
    def get_token_usage_stats(self) -> Dict[str, int]:
        """
        Get current token usage statistics for this client instance.
        
        Returns:
            Dict containing token usage statistics
        """
        return {
            "total_prompt_tokens": self.total_prompt_tokens,
            "total_completion_tokens": self.total_completion_tokens,
            "total_tokens": self.total_tokens,
            "api_call_count": self.api_call_count,
            "average_prompt_tokens": self.total_prompt_tokens // max(1, self.api_call_count),
            "average_completion_tokens": self.total_completion_tokens // max(1, self.api_call_count),
            "average_total_tokens": self.total_tokens // max(1, self.api_call_count)
        }
    
    def get_usage_since_last_reset(self) -> Dict[str, Any]:
        """
        Get usage statistics with provider information for cost tracking.
        
        Returns:
            Dict containing detailed usage statistics including provider info
        """
        stats = self.get_token_usage_stats()
        stats["last_provider_used"] = getattr(self, '_last_provider_used', 'unknown')
        stats["last_model_used"] = getattr(self, '_last_model_used', 'unknown')
        return stats
    
    def reset_token_usage_stats(self) -> Dict[str, int]:
        """
        Reset token usage statistics and return the previous totals.
        
        Returns:
            Dict containing the previous token usage statistics
        """
        previous_stats = self.get_token_usage_stats()
        
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.total_tokens = 0
        self.api_call_count = 0
        
        self.logger.info("Token usage statistics reset")
        return previous_stats
    
    def create_completion(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        max_tokens: int = 10000,
        response_format: Optional[Dict[str, Any]] = None,
        temperature: float = 0.7,
        clean_response: bool = True,
        **kwargs
    ) -> Any:
        """
        Create a chat completion with automatic retry logic and provider fallback using OpenAI standards.
        
        Args:
            messages: List of message dictionaries with 'role' and 'content'
            model: Model to use (if None, uses default model for each provider)
            max_tokens: Maximum tokens to generate
            response_format: Response format specification for structured output
            temperature: Sampling temperature
            clean_response: Whether to clean the response content
            **kwargs: Additional parameters to pass to the API
            
        Returns:
            OpenAI ChatCompletion response object
            
        Raises:
            RuntimeError: If all providers and retry attempts fail
        """
        self.logger.debug(f"Starting completion with model: {model}, max_tokens: {max_tokens}, temperature: {temperature}")
        self.logger.debug(f"Message count: {len(messages)}")
        
        last_exception = None
        
        for provider_idx, provider_client in enumerate(self.clients):
            provider_name = provider_client["name"]
            client = provider_client["client"]
            default_model = provider_client["default_model"]
            
            # Use provided model or fall back to provider's default
            current_model = model if model else default_model
            
            self.logger.info(f"Trying provider {provider_idx + 1}/{len(self.clients)}: {provider_name} with model: {current_model}")
            
            for attempt in range(self.max_retries):
                try:
                    self.logger.debug(f"Provider {provider_name} attempt {attempt + 1}/{self.max_retries}")
                    
                    completion_args = {
                        "messages": messages,
                        "model": current_model,
                        "max_tokens": max_tokens,
                        "temperature": temperature,
                        **kwargs
                    }
                    
                    if response_format:
                        completion_args["response_format"] = response_format
                        self.logger.debug(f"Using response format: {response_format}")
                    
                    result = client.chat.completions.create(**completion_args)
                    
                    # Add provider info to result
                    result.provider_used = provider_name
                    result.model_used = current_model
                    
                    # Track for cost calculation
                    self._last_provider_used = provider_name
                    self._last_model_used = current_model
                    
                    self._track_token_usage(result)
                    
                    # Check if we actually got content - if not, treat as failure
                    if result.choices and len(result.choices) > 0:
                        self.logger.debug(f"Provider {provider_name} returned choices: {result}")
                        original_content = result.choices[0].message.content
                        if original_content is None:
                            self.logger.warning(f"Provider {provider_name} returned None content - treating as failure")
                            raise RuntimeError(f"Provider {provider_name} returned None content")
                    else:
                        self.logger.warning(f"Provider {provider_name} returned no choices - treating as failure")
                        raise RuntimeError(f"Provider {provider_name} returned no choices")
                    
                    if clean_response and result.choices and len(result.choices) > 0:
                        original_content = result.choices[0].message.content
                        cleaned_content = self.clean_response_content(original_content)
                        result.choices[0].message.content = cleaned_content
                    
                    self.logger.info(f"Successfully created completion using {provider_name}")
                    return result
                    
                except Exception as e:
                    last_exception = e
                    self.logger.warning(f"Provider {provider_name} attempt {attempt + 1} failed: {str(e)}")
                    
                    if attempt < self.max_retries - 1:
                        delay = self.base_delay * (2 ** attempt)
                        self.logger.debug(f"Retrying {provider_name} in {delay} seconds...")
                        time.sleep(delay)
                    else:
                        self.logger.warning(f"Provider {provider_name} failed after {self.max_retries} attempts")
                        break
            
            # If we should fallback and there are more providers, continue to next provider
            if self.fallback_on_provider_failure and provider_idx < len(self.clients) - 1:
                self.logger.info(f"Falling back from {provider_name} to next provider")
                continue
            else:
                # If no fallback or this is the last provider, break
                break
        
        # If we get here, all providers failed
        self.logger.error(f"All providers failed after {self.max_retries} attempts each")
        raise RuntimeError(f"Failed to create completion using all {len(self.clients)} providers. Last error: {str(last_exception)}")
    
    def create_structured_completion(
        self,
        messages: List[Dict[str, str]],
        response_schema: BaseModel,
        model: Optional[str] = None,
        max_tokens: int = 3000,
        **kwargs
    ) -> BaseModel:
        """
        Create a completion that returns a structured response based on a Pydantic model using OpenAI standards.
        
        Args:
            messages: List of message dictionaries
            response_schema: Pydantic model class for the expected response
            model: Model to use (if None, uses default model for each provider)
            max_tokens: Maximum tokens to generate
            **kwargs: Additional parameters
            
        Returns:
            Instance of the response_schema model
            
        Raises:
            RuntimeError: If completion fails or response doesn't match schema
        """
        self.logger.debug(f"Creating structured completion with schema: {response_schema.__name__}")
        
        try:
            schema = response_schema.model_json_schema()
            
            result = self.create_completion(
                messages=messages,
                model=model,
                max_tokens=max_tokens,
                temperature=0,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": response_schema.__name__,
                        "schema": schema,
                        "strict": True
                    }
                },
                **kwargs
            )
            
            content = result.choices[0].message.content
            self.logger.debug(f"Raw JSON response: {content}")
            
            parsed_json = json.loads(content)
            self.logger.debug(f"Parsed JSON: {parsed_json}")
            
            structured_response = response_schema(**parsed_json)
            self.logger.debug(f"Successfully created structured response of type: {type(structured_response)}")
            self.logger.debug(f"Used provider: {getattr(result, 'provider_used', 'unknown')}")
            return structured_response
            
        except (json.JSONDecodeError, ValueError) as e:
            self.logger.error(f"Failed to parse structured response: {str(e)}")
            raise RuntimeError(f"Failed to parse structured response: {str(e)}")
    
    def create_completion_with_structural_reflection(
        self,
        messages: List[Dict[str, str]],
        reflection_criteria: str,
        model: Optional[str] = None,
        max_tokens: int = 3000,
        max_iterations: int = 3,
        reflection_model: Optional[str] = None,
        temperature: float = 0.0,
        **kwargs
    ) -> Any:
        """
        Create a completion with reflection and regeneration workflow using OpenAI standards.
        
        This method:
        1. Generates an initial response
        2. Reflects on the quality using specified criteria
        3. Regenerates if the reflection indicates issues
        4. Repeats until satisfactory or max iterations reached
        
        Args:
            messages: List of message dictionaries for the main completion
            reflection_criteria: Criteria for evaluating response quality
            model: Model to use for main completion (if None, uses default model for each provider)
            max_tokens: Maximum tokens for main completion
            max_iterations: Maximum number of regeneration attempts
            reflection_model: Model to use for reflection (if None, uses same as main model)
            temperature: Temperature for main completion
            **kwargs: Additional parameters for main completion
            
        Returns:
            OpenAI ChatCompletion response object with additional metadata
            
        Raises:
            RuntimeError: If all attempts fail
        """
        self.logger.info(f"Starting completion with reflection (max {max_iterations} iterations)")
        self.logger.debug(f"Main model: {model}, Reflection model: {reflection_model}")
        self.logger.debug(f"Reflection criteria: {reflection_criteria}")
        self.logger.debug(f"Temperature: {temperature}, Max tokens: {max_tokens}")
        
        original_response = None
                
        for iteration in range(max_iterations):
            try:
                self.logger.info(f"Generation iteration {iteration + 1}/{max_iterations}")
                response = self.create_completion(
                    messages=messages,
                    model=model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    **kwargs
                )

                if original_response is None:
                    original_response = response
                
                generated_content = response.choices[0].message.content
                self.logger.debug(f"Generated content length: {len(generated_content)}")
                
                if iteration == max_iterations - 1:
                    self.logger.info("Final iteration reached, returning response.")
                    self.logger.warning(f"Reached maximum iterations ({max_iterations}). Returning response.")

                    return original_response if original_response is not None else response
                
                reflection_messages = [
                    {
                        "role": "system",
                        "content": "You are a formatting and structure evaluator. Your primary job is to assess how well the generated content follows the specified formatting requirements and structural criteria. Focus on format compliance, not content quality."
                    },
                    {
                        "role": "user",
                        "content": f"""
Please evaluate the following generated content based on these FORMATTING CRITERIA:
Focus PRIMARILY on formatting, structure, and presentation - not on content quality or accuracy.

FORMATTING CRITERIA:
{reflection_criteria}

GENERATED CONTENT:
{generated_content}

ORIGINAL PROMPT:
{messages[-1]["content"]}

Respond with a JSON object containing:
- "criteria_adherence_score": integer from 1-10 (10 being perfect formatting adherence)
- "meets_criteria": boolean (true if content meets formatting requirements)
- "issues_found": array of specific FORMATTING issues identified (e.g., missing headers, incorrect structure, wrong format)
- "improvement_suggestions": array of specific FORMATTING suggestions for improvement (e.g., add missing sections, fix header levels, adjust structure)

Evaluate ONLY formatting and structural adherence - ignore content quality, accuracy, or completeness. Be lenient on content while strict on format."""
                    }
                ]
                
                self.logger.info("Performing reflection on generated content")
                self.logger.debug(f"Reflection message count: {len(reflection_messages)}")
                
                reflection_data = self.create_structured_completion(
                    messages=reflection_messages,
                    response_schema=ReflectionResponse,
                    model=reflection_model,
                    max_tokens=1000
                )
                
                criteria_adherence_score = reflection_data.criteria_adherence_score
                meets_criteria = reflection_data.meets_criteria
                issues_found = reflection_data.issues_found
                improvement_suggestions = reflection_data.improvement_suggestions
                
                self.logger.info(f"Reflection results - Formatting adherence score: {criteria_adherence_score}/10, Meets formatting criteria: {meets_criteria}")
                
                if meets_criteria and criteria_adherence_score >= 7:
                    self.logger.info("Content meets formatting criteria and threshold, returning response")
                    response.reflection_metadata = {
                        "iterations": iteration + 1,
                        "final_criteria_adherence_score": criteria_adherence_score,
                        "meets_criteria": meets_criteria,
                        "reflection_data": reflection_data.model_dump()
                    }
                    return response
                
                self.logger.debug(f"Issues found: {issues_found}")
                self.logger.debug(f"Improvement suggestions: {improvement_suggestions}")

                improvement_context = "\n".join([
                    "PREVIOUS FORMATTING ISSUES:",
                    *[f"- {issue}" for issue in issues_found],
                    "\nFORMATTING IMPROVEMENT SUGGESTIONS:",
                    *[f"- {suggestion}" for suggestion in improvement_suggestions],
                    "\nPlease fix these FORMATTING issues in your response while keeping the content intact.",
                    "\nFocus only on formatting and structure corrections. Do not include formatting notes, rationale or any response to the feedback."
                ])
                
                self.logger.debug(f"Adding improvement context: {improvement_context}")
                
                enhanced_messages = messages.copy()
                if enhanced_messages:
                    enhanced_messages[-1]["content"] += f"\n\n{improvement_context}"
                
                messages = enhanced_messages
                self.logger.debug("Enhanced messages with improvement context for next iteration")
                
                self.logger.info(f"Formatting adherence insufficient (score: {criteria_adherence_score}), regenerating...")
                    
            except Exception as e:
                self.logger.error(f"Error in reflection iteration {iteration + 1}: {str(e)}")
                if iteration == max_iterations - 1:
                    raise RuntimeError(f"Reflection workflow failed after {max_iterations} attempts. Last error: {str(e)}")
        
        raise RuntimeError(f"Unexpected end of reflection workflow after {max_iterations} iterations")

    @classmethod
    def create_gemini_with_together_fallback(
        cls,
        gemini_api_key: Optional[str] = None,
        together_api_key: Optional[str] = None,
        max_retries: int = 3,
        base_delay: float = 1.0
    ) -> 'LLMClient':
        """
        Convenience method to create an LLMClient with Gemini as primary and TogetherAI as fallback.
        
        Args:
            gemini_api_key: Gemini API key (if None, uses GEMINI_API_KEY env var)
            together_api_key: TogetherAI API key (if None, uses TOGETHER_API_KEY env var)
            max_retries: Maximum retry attempts per provider
            base_delay: Base delay for exponential backoff
            
        Returns:
            LLMClient instance configured with Gemini + TogetherAI fallback
        """
        providers = [
            {
                "name": "gemini",
                "api_key": gemini_api_key or os.getenv("GEMINI_API_KEY"),
                "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
                "default_model": "gemini-2.5-flash",
                "priority": 1
            },
            {
                "name": "together",
                "api_key": together_api_key or os.getenv("TOGETHER_API_KEY"),
                "base_url": "https://api.together.xyz/v1",
                "default_model": "deepseek-ai/DeepSeek-V3",
                "priority": 2
            }
        ]
        
        return cls(
            providers=providers,
            max_retries=max_retries,
            base_delay=base_delay,
            fallback_on_provider_failure=True
        )
    
    def test_provider_connectivity(self) -> Dict[str, Any]:
        """
        Test connectivity to all configured providers.
        
        Returns:
            Dict with connectivity status for each provider
        """
        results = {
            "total_providers": len(self.clients),
            "providers": []
        }
        
        for provider_client in self.clients:
            provider_name = provider_client["name"]
            client = provider_client["client"]
            default_model = provider_client["default_model"]
            
            self.logger.info(f"Testing connectivity for provider: {provider_name}")
            
            try:
                # Simple test completion
                response = client.chat.completions.create(
                    messages=[
                        {"role": "system", "content": "You are a helpful assistant."},
                        {"role": "user", "content": "Say 'Hello' in one word."}
                    ],
                    model=default_model,
                    max_tokens=10,
                    temperature=0
                )
                
                status = "connected"
                error = None
                response_content = response.choices[0].message.content if response.choices else ""
                
                self.logger.info(f"Provider {provider_name} connectivity test successful")
                
            except Exception as e:
                status = "failed"
                error = str(e)
                response_content = None
                
                self.logger.warning(f"Provider {provider_name} connectivity test failed: {error}")
            
            results["providers"].append({
                "name": provider_name,
                "default_model": default_model,
                "status": status,
                "error": error,
                "test_response": response_content
            })
        
        return results


if __name__ == "__main__":
    # Test basic completion
    llm_client = LLMClient()
    
    print("=== Provider Information ===")
    provider_info = llm_client.get_current_provider_info()
    print(f"Total providers: {provider_info['total_providers']}")
    for provider in provider_info['providers']:
        print(f"  - {provider['name']}: {provider['default_model']}")
    
    print("\n=== Connectivity Test ===")
    connectivity_results = llm_client.test_provider_connectivity()
    for provider in connectivity_results['providers']:
        status_icon = "✅" if provider['status'] == 'connected' else "❌"
        print(f"{status_icon} {provider['name']}: {provider['status']}")
        if provider['error']:
            print(f"    Error: {provider['error']}")
        if provider['test_response']:
            print(f"    Response: {provider['test_response']}")
    
    print("\n=== Token Usage Stats (Initial) ===")
    print(llm_client.get_token_usage_stats())
    
    print("\n=== Basic Completion Test ===")
    result = llm_client.create_completion(
        messages=[
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "What is the capital of France?"}
        ]
    )
    print(f"Provider used: {getattr(result, 'provider_used', 'unknown')}")
    print(f"Model used: {getattr(result, 'model_used', 'unknown')}")
    print(result.choices[0].message.content)

    # Test structured completion
    print("\n=== Structured Completion Test ===")
    
    class CityInfo(BaseModel):
        city: str
        country: str
        population: int
        famous_landmarks: List[str]
        
    structured_result = llm_client.create_structured_completion(
        messages=[
            {"role": "system", "content": "You are a geography expert."},
            {"role": "user", "content": "Tell me about Paris, France including its population and famous landmarks."}
        ],
        response_schema=CityInfo
    )
    
    print("Structured Response:")
    print(f"City: {structured_result.city}")
    print(f"Country: {structured_result.country}")
    print(f"Population: {structured_result.population:,}")
    print(f"Famous Landmarks: {', '.join(structured_result.famous_landmarks)}")

    # Test completion with reflection
    print("\n=== Completion with Reflection Test ===")
    result = llm_client.create_completion_with_structural_reflection(
        messages=[
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "What is the capital of France?"}
        ],
        reflection_criteria="""
        The response should be in the following format:
        # [COUNTRY_NAME]
        ## [COUNTRY_CAPITAL]: information about the capital
        ## additional information about the country
        """,
    )
    print(f"Provider used: {getattr(result, 'provider_used', 'unknown')}")
    print(f"Model used: {getattr(result, 'model_used', 'unknown')}")
    print(result.choices[0].message.content)
    
    print("\n=== Final Token Usage Stats ===")
    print(llm_client.get_token_usage_stats())
    
    print("\n=== Test Convenience Method ===")
    simple_client = LLMClient.create_gemini_with_together_fallback()
    print(f"Simple client has {len(simple_client.clients)} providers configured")
