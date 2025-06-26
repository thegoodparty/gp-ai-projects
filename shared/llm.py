import os
import json
import time
from typing import Optional, Dict, Any, List
from together import Together
from dotenv import load_dotenv
from pydantic import BaseModel

from shared.logger import get_logger

load_dotenv()


class LLMClient:
    """
    A client class that abstracts TogetherAI interactions with built-in retry logic.
    """
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        default_model: str = "Qwen/Qwen3-235B-A22B-fp8-tput",
        max_retries: int = 5,
        base_delay: float = 1.0
    ):
        """
        Initialize the LLM client.
        
        Args:
            api_key: TogetherAI API key. If None, will use TOGETHER_API_KEY env var
            default_model: Default model to use for completions
            max_retries: Maximum number of retry attempts
            base_delay: Base delay for exponential backoff (seconds)
        """
        self.api_key = api_key or os.getenv("TOGETHER_API_KEY")
        self.default_model = default_model
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.logger = get_logger(__name__)
        
        if not self.api_key:
            raise ValueError("TogetherAI API key must be provided either as parameter or TOGETHER_API_KEY env var")
        
        self.client = Together(api_key=self.api_key)
    
    def create_completion(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        max_tokens: int = 300,
        response_format: Optional[Dict[str, Any]] = None,
        temperature: float = 0.7,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Create a chat completion with automatic retry logic.
        
        Args:
            messages: List of message dictionaries with 'role' and 'content'
            model: Model to use (defaults to default_model)
            max_tokens: Maximum tokens to generate
            response_format: Response format specification for structured output
            temperature: Sampling temperature
            **kwargs: Additional parameters to pass to the API
            
        Returns:
            Dict containing the API response
            
        Raises:
            RuntimeError: If all retry attempts fail
        """
        model = model or self.default_model
        
        for attempt in range(self.max_retries):
            try:
                self.logger.info(f"Creating completion (attempt {attempt + 1}/{self.max_retries})")
                
                completion_args = {
                    "messages": messages,
                    "model": model,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                    **kwargs
                }
                
                if response_format:
                    completion_args["response_format"] = response_format
                
                result = self.client.chat.completions.create(**completion_args)
                
                self.logger.info("Successfully created completion")
                return result
                
            except Exception as e:
                self.logger.warning(f"Attempt {attempt + 1} failed with error: {str(e)}")
                
                if attempt < self.max_retries - 1:
                    delay = self.base_delay * (2 ** attempt)
                    self.logger.info(f"Retrying in {delay} seconds...")
                    time.sleep(delay)
                else:
                    self.logger.error(f"All {self.max_retries} attempts failed.")
                    raise RuntimeError(f"Failed to create completion after {self.max_retries} attempts. Last error: {str(e)}")
    
    def create_structured_completion(
        self,
        messages: List[Dict[str, str]],
        response_schema: BaseModel,
        model: Optional[str] = None,
        max_tokens: int = 300,
        **kwargs
    ) -> BaseModel:
        """
        Create a completion that returns a structured response based on a Pydantic model.
        
        Args:
            messages: List of message dictionaries
            response_schema: Pydantic model class for the expected response
            model: Model to use
            max_tokens: Maximum tokens to generate
            **kwargs: Additional parameters
            
        Returns:
            Instance of the response_schema model
            
        Raises:
            RuntimeError: If completion fails or response doesn't match schema
        """
        try:
            result = self.create_completion(
                messages=messages,
                model=model,
                max_tokens=max_tokens,
                response_format={
                    "type": "json_schema",
                    "schema": response_schema.model_json_schema(),
                },
                **kwargs
            )
            
            content = result.choices[0].message.content
            parsed_json = json.loads(content)
            return response_schema(**parsed_json)
            
        except (json.JSONDecodeError, ValueError) as e:
            self.logger.error(f"Failed to parse structured response: {str(e)}")
            raise RuntimeError(f"Failed to parse structured response: {str(e)}")
