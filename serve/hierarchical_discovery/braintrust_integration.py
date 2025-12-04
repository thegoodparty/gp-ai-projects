#!/usr/bin/env python3
"""
Braintrust Integration Module for Hierarchical Discovery Pipeline

This module provides integration with Braintrust for:
1. Logging/tracing LLM calls for evaluation
2. Loading prompts from Braintrust (with local fallback)
3. Wrapping existing LLM clients to automatically log

Usage:
    # Initialize at the start of your script
    from serve.hierarchical_discovery.braintrust_integration import (
        init_braintrust,
        traced_llm_call,
        load_prompt_from_braintrust,
        BraintrustConfig
    )
    
    # Initialize Braintrust logging
    init_braintrust(project="hierarchical-discovery")
    
    # Wrap your LLM calls
    response = traced_llm_call(
        name="cluster_analysis",
        prompt=prompt,
        llm_call_fn=lambda: client.generate_structured_content(prompt=prompt, ...),
        metadata={"cluster_id": cluster_id}
    )

Environment Variables:
    BRAINTRUST_API_KEY: Your Braintrust API key
    BRAINTRUST_PROJECT: Default project name (optional)
"""

import os
import json
import functools
from typing import Optional, Dict, Any, Callable, TypeVar
from dataclasses import dataclass, field
from datetime import datetime

from dotenv import load_dotenv
from shared.logger import get_logger

# Load environment variables from .env file
load_dotenv()

logger = get_logger(__name__)

# Type variable for generic return type
T = TypeVar('T')

# Global state
_braintrust_enabled = False
_braintrust_logger = None


@dataclass
class BraintrustConfig:
    """Configuration for Braintrust integration"""
    project: str = "hierarchical-discovery"
    api_key: Optional[str] = None
    enabled: bool = True
    log_inputs: bool = True
    log_outputs: bool = True
    log_metadata: bool = True
    
    def __post_init__(self):
        if self.api_key is None:
            self.api_key = os.getenv("BRAINTRUST_API_KEY")


def init_braintrust(
    project: Optional[str] = None,
    api_key: Optional[str] = None,
    enabled: bool = True
) -> bool:
    """
    Initialize Braintrust logging.
    
    Args:
        project: Braintrust project name
        api_key: Braintrust API key (uses env var if not provided)
        enabled: Whether to enable logging (useful for toggling in dev)
    
    Returns:
        True if successfully initialized, False otherwise
    """
    global _braintrust_enabled, _braintrust_logger
    
    if not enabled:
        logger.info("Braintrust integration disabled")
        _braintrust_enabled = False
        return False
    
    api_key = api_key or os.getenv("BRAINTRUST_API_KEY")
    project = project or os.getenv("BRAINTRUST_PROJECT", "hierarchical-discovery")
    
    if not api_key:
        logger.warning("BRAINTRUST_API_KEY not set. Braintrust logging disabled.")
        _braintrust_enabled = False
        return False
    
    try:
        import braintrust
        
        # Initialize the logger for production logging
        _braintrust_logger = braintrust.init_logger(
            project=project,
            api_key=api_key
        )
        
        _braintrust_enabled = True
        logger.info(f"Braintrust initialized for project: {project}")
        return True
        
    except ImportError:
        logger.warning("braintrust package not installed. Run: pip install braintrust")
        _braintrust_enabled = False
        return False
    except Exception as e:
        logger.error(f"Failed to initialize Braintrust: {e}")
        _braintrust_enabled = False
        return False


def traced_llm_call(
    name: str,
    input_data: Dict[str, Any],
    llm_call_fn: Callable[[], T],
    prompt: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    tags: Optional[list] = None
) -> T:
    """
    Execute an LLM call with Braintrust tracing.
    
    Logs clean structured input/output to Braintrust for easy evaluation.
    
    Args:
        name: Name for this trace (e.g., "cluster_analysis")
        input_data: Structured input data (what we're analyzing) - this shows in Braintrust UI
        llm_call_fn: A callable that executes the actual LLM call
        prompt: The actual prompt sent to LLM (stored in metadata for debugging)
        metadata: Additional metadata to log
        tags: Tags to categorize this trace
    
    Returns:
        The result from llm_call_fn
    """
    global _braintrust_enabled, _braintrust_logger
    
    start_time = datetime.now()
    
    # If Braintrust not enabled, just execute the function
    if not _braintrust_enabled or _braintrust_logger is None:
        return llm_call_fn()
    
    try:
        import braintrust
        
        # Start a span for this LLM call
        with _braintrust_logger.start_span(name=name) as span:
            # Execute the actual LLM call
            result = llm_call_fn()
            
            # Calculate duration
            end_time = datetime.now()
            duration_ms = (end_time - start_time).total_seconds() * 1000
            
            # Handle different result types for output
            if hasattr(result, 'model_dump'):
                output_data = result.model_dump()
            elif hasattr(result, '__dict__'):
                output_data = result.__dict__
            else:
                output_data = {"result": str(result)}
            
            # Build metadata
            log_metadata = {
                **(metadata or {}),
                "duration_ms": round(duration_ms, 2)
            }
            
            # Store prompt in metadata (for debugging) not in input
            if prompt:
                log_metadata["prompt"] = prompt
            
            # Log clean structured data
            span.log(
                input=input_data,  # Clean structured input (cluster_info, messages)
                output=output_data,  # Clean structured output (theme, summary, analysis)
                tags=tags or [],
                metadata=log_metadata
            )
            
            return result
            
    except Exception as e:
        logger.error(f"Error in traced_llm_call: {e}")
        # Still execute the LLM call even if tracing fails
        return llm_call_fn()


def load_prompt_from_braintrust(
    prompt_name: str,
    fallback_prompt: str,
    variables: Optional[Dict[str, Any]] = None
) -> str:
    """
    Load a prompt from Braintrust, with local fallback.
    
    This allows you to manage prompts in Braintrust's UI and iterate on them
    without code changes. If Braintrust is not available or the prompt doesn't
    exist, it falls back to the provided local prompt.
    
    Args:
        prompt_name: The name/slug of the prompt in Braintrust
        fallback_prompt: Local prompt string to use if Braintrust unavailable
        variables: Variables to interpolate into the prompt template
    
    Returns:
        The rendered prompt string
    
    Example:
        prompt = load_prompt_from_braintrust(
            prompt_name="cluster-analysis-v1",
            fallback_prompt=self._create_local_prompt(cluster_id, examples),
            variables={"cluster_id": 5, "examples": examples_text}
        )
    """
    global _braintrust_enabled
    
    if not _braintrust_enabled:
        logger.debug(f"Braintrust not enabled, using fallback prompt for: {prompt_name}")
        return _render_prompt(fallback_prompt, variables)
    
    try:
        import braintrust
        
        # Load the prompt from Braintrust
        prompt = braintrust.load_prompt(
            project=os.getenv("BRAINTRUST_PROJECT", "hierarchical-discovery"),
            slug=prompt_name
        )
        
        if prompt is None:
            logger.debug(f"Prompt '{prompt_name}' not found in Braintrust, using fallback")
            return _render_prompt(fallback_prompt, variables)
        
        # Render the prompt with variables
        rendered = prompt.build(**(variables or {}))
        logger.debug(f"Loaded prompt '{prompt_name}' from Braintrust")
        
        # Return just the prompt content if it's a simple string
        if isinstance(rendered, str):
            return rendered
        
        # Handle structured prompt responses
        if hasattr(rendered, 'messages') and rendered.messages:
            # Extract content from messages
            return "\n".join(
                msg.get('content', '') if isinstance(msg, dict) else str(msg)
                for msg in rendered.messages
            )
        
        return str(rendered)
        
    except Exception as e:
        logger.warning(f"Failed to load prompt '{prompt_name}' from Braintrust: {e}")
        return _render_prompt(fallback_prompt, variables)


def _render_prompt(prompt: str, variables: Optional[Dict[str, Any]]) -> str:
    """Render a prompt template with variables using simple string formatting."""
    if not variables:
        return prompt
    
    try:
        # Try .format() style rendering
        return prompt.format(**variables)
    except (KeyError, ValueError):
        # If format fails, return as-is (prompt might not be a template)
        return prompt


def traced(name: Optional[str] = None, tags: Optional[list] = None):
    """
    Decorator to trace a function with Braintrust.
    
    Args:
        name: Name for this trace (defaults to function name)
        tags: Tags to categorize this trace
    
    Example:
        @traced(name="analyze_cluster", tags=["analysis"])
        async def analyze_cluster(self, cluster_id: int, messages: list):
            ...
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            trace_name = name or func.__name__
            
            if not _braintrust_enabled or _braintrust_logger is None:
                return func(*args, **kwargs)
            
            try:
                import braintrust
                
                with _braintrust_logger.start_span(name=trace_name) as span:
                    # Log inputs (excluding self for methods)
                    input_args = args[1:] if args and hasattr(args[0], '__class__') else args
                    span.log(
                        input={"args": str(input_args)[:1000], "kwargs": str(kwargs)[:1000]},
                        metadata={"tags": tags or []}
                    )
                    
                    result = func(*args, **kwargs)
                    
                    # Log output
                    span.log(output=str(result)[:2000] if result else None)
                    
                    return result
                    
            except Exception as e:
                logger.error(f"Error in @traced decorator: {e}")
                return func(*args, **kwargs)
        
        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            trace_name = name or func.__name__
            
            if not _braintrust_enabled or _braintrust_logger is None:
                return await func(*args, **kwargs)
            
            try:
                import braintrust
                
                with _braintrust_logger.start_span(name=trace_name) as span:
                    input_args = args[1:] if args and hasattr(args[0], '__class__') else args
                    span.log(
                        input={"args": str(input_args)[:1000], "kwargs": str(kwargs)[:1000]},
                        metadata={"tags": tags or []}
                    )
                    
                    result = await func(*args, **kwargs)
                    
                    span.log(output=str(result)[:2000] if result else None)
                    
                    return result
                    
            except Exception as e:
                logger.error(f"Error in @traced decorator: {e}")
                return await func(*args, **kwargs)
        
        # Return appropriate wrapper based on function type
        import asyncio
        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        return wrapper
    
    return decorator


def flush_logs():
    """Flush any pending logs to Braintrust."""
    global _braintrust_logger
    
    if _braintrust_logger is not None:
        try:
            _braintrust_logger.flush()
            logger.debug("Flushed Braintrust logs")
        except Exception as e:
            logger.error(f"Failed to flush Braintrust logs: {e}")


def is_enabled() -> bool:
    """Check if Braintrust logging is enabled."""
    return _braintrust_enabled


# ============================================================================
# PROMPT TEMPLATES
# These are the default prompts that can be overridden in Braintrust
# ============================================================================

CLUSTER_ANALYSIS_PROMPT_TEMPLATE = """Analyze this cluster of civic engagement messages from political campaigns.

INPUT:
{input_yaml}

Provide an analysis with these three fields:

1. **Theme**: Create a concise 2-4 word theme/label that captures the essence of this cluster

2. **Issues Summary**: Write 1 sentence describing the core issues or concerns people are expressing

3. **Detailed Analysis**: Write 2-3 paragraphs analyzing common concerns, patterns, underlying issues, and what citizens are experiencing. Focus on the problems, frustrations, or needs expressed."""

