from datetime import date
import os
import json
import time
import re
from typing import Optional, Dict, Any, List
from together import Together
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
        
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.total_tokens = 0
        self.api_call_count = 0
        
        if not self.api_key:
            raise ValueError("TogetherAI API key must be provided either as parameter or TOGETHER_API_KEY env var")
        
        self.client = Together(api_key=self.api_key)
    
    def clean_response_content(self, content: str) -> str:
        """
        Clean LLM response content by removing think tags and other unwanted patterns.
        
        Args:
            content: Raw content from LLM response
            
        Returns:
            str: Cleaned content
        """
        self.logger.debug(f"Cleaning response content, original length: {len(content)}")
        cleaned_content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
        if len(cleaned_content) != len(content):
            self.logger.debug(f"Content cleaned, new length: {len(cleaned_content)}")
        return cleaned_content
    
    def _track_token_usage(self, result) -> None:
        """
        Track token usage from API response and update running totals.
        
        Args:
            result: API response object from Together
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
    ) -> Dict[str, Any]:
        """
        Create a chat completion with automatic retry logic.
        
        Args:
            messages: List of message dictionaries with 'role' and 'content'
            model: Model to use (defaults to default_model)
            max_tokens: Maximum tokens to generate
            response_format: Response format specification for structured output
            temperature: Sampling temperature
            clean_response: Whether to clean the response content
            **kwargs: Additional parameters to pass to the API
            
        Returns:
            Dict containing the API response
            
        Raises:
            RuntimeError: If all retry attempts fail
        """
        model = model or self.default_model
        
        self.logger.debug(f"Starting completion with model: {model}, max_tokens: {max_tokens}, temperature: {temperature}")
        self.logger.debug(f"Message count: {len(messages)}")
        
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
                    self.logger.debug(f"Using response format: {response_format}")
                
                self.logger.debug(f"Completion arguments: {completion_args}")
                result = self.client.chat.completions.create(**completion_args)
                
                self._track_token_usage(result)
                
                if clean_response and result.choices and len(result.choices) > 0:
                    original_content = result.choices[0].message.content
                    self.logger.debug("Cleaning response content")
                    cleaned_content = self.clean_response_content(original_content)
                    result.choices[0].message.content = cleaned_content
                
                self.logger.debug(f"Completion successful, response length: {len(result.choices[0].message.content) if result.choices else 0}")
                self.logger.debug(f"Response: {result.choices[0].message.content}")
                self.logger.info("Successfully created completion")
                return result
                
            except Exception as e:
                self.logger.warning(f"Attempt {attempt + 1} failed with error: {str(e)}")
                self.logger.debug(f"Exception type: {type(e).__name__}")
                
                if attempt < self.max_retries - 1:
                    delay = self.base_delay * (2 ** attempt)
                    self.logger.info(f"Retrying in {delay} seconds...")
                    self.logger.debug(f"Exponential backoff delay: {delay}s for attempt {attempt + 1}")
                    time.sleep(delay)
                else:
                    self.logger.error(f"All {self.max_retries} attempts failed.")
                    self.logger.debug(f"Final failure details: {str(e)}")
                    raise RuntimeError(f"Failed to create completion after {self.max_retries} attempts. Last error: {str(e)}")
    
    def create_structured_completion(
        self,
        messages: List[Dict[str, str]],
        response_schema: BaseModel,
        model: Optional[str] = "meta-llama/Llama-4-Scout-17B-16E-Instruct",
        max_tokens: int = 3000,
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
        self.logger.debug(f"Creating structured completion with schema: {response_schema.__name__}")
        
        try:
            schema = response_schema.model_json_schema()
            self.logger.debug(f"JSON schema: {json.dumps(schema, indent=2)}")
            
            result = self.create_completion(
                messages=messages,
                model=model,
                max_tokens=max_tokens,
                temperature=0.0,
                response_format={
                    "type": "json_schema",
                    "schema": schema,
                },
                **kwargs
            )
            
            content = result.choices[0].message.content
            self.logger.debug(f"Raw JSON response: {content}")
            
            parsed_json = json.loads(content)
            self.logger.debug(f"Parsed JSON: {parsed_json}")
            
            structured_response = response_schema(**parsed_json)
            self.logger.debug(f"Successfully created structured response of type: {type(structured_response)}")
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
        reflection_model: str = "deepseek-ai/DeepSeek-R1",
        temperature: float = 0.0,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Create a completion with reflection and regeneration workflow.
        
        This method:
        1. Generates an initial response
        2. Reflects on the quality using specified criteria
        3. Regenerates if the reflection indicates issues
        4. Repeats until satisfactory or max iterations reached
        
        Args:
            messages: List of message dictionaries for the main completion
            reflection_criteria: Criteria for evaluating response quality
            model: Model to use for main completion
            max_tokens: Maximum tokens for main completion
            max_iterations: Maximum number of regeneration attempts
            reflection_model: Model to use for reflection (defaults to main model)
            temperature: Temperature for main completion
            **kwargs: Additional parameters for main completion
            
        Returns:
            Dict containing the final API response with additional metadata
            
        Raises:
            RuntimeError: If all attempts fail
        """
        model = model or self.default_model
        
        self.logger.info(f"Starting completion with reflection (max {max_iterations} iterations)")
        self.logger.debug(f"Main model: {model}, Reflection model: {reflection_model}")
        self.logger.debug(f"Reflection criteria: {reflection_criteria}")
        self.logger.debug(f"Temperature: {temperature}, Max tokens: {max_tokens}")
                
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

                print(f"iteration: {iteration}")
                if iteration == 0:
                    original_response = response
                
                generated_content = response.choices[0].message.content
                self.logger.debug(f"Generated content length: {len(generated_content)}")
                
                if iteration == max_iterations - 1:
                    self.logger.info("Final iteration reached, returning original response.")
                    self.logger.warning(f"Reached maximum iterations ({max_iterations}). Returning the original response.")

                    return original_response
                
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


if __name__ == "__main__":
    llm_client = LLMClient()
    print(llm_client.get_token_usage_stats())
    result = llm_client.create_completion(
        messages=[
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "What is the capital of France?"}
        ]
    )
    print(result.choices[0].message.content)

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
    print(result.choices[0].message.content)
    print(llm_client.get_token_usage_stats())
