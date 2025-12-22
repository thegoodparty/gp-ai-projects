#!/usr/bin/env python3

import os
import threading
from typing import Optional, Dict, Any, Callable, TypeVar

from dotenv import load_dotenv
from shared.logger import get_logger

load_dotenv()

logger = get_logger(__name__)

T = TypeVar('T')


class BraintrustClient:
    _instance: Optional['BraintrustClient'] = None
    _lock = threading.Lock()

    def __init__(self):
        self._braintrust_logger = None
        self._braintrust_module = None
        self._enabled = False
        self._project: Optional[str] = None
        self._initialized = False

    @classmethod
    def get_instance(cls) -> 'BraintrustClient':
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        with cls._lock:
            if cls._instance is not None:
                cls._instance._cleanup()
            cls._instance = None

    def _cleanup(self) -> None:
        if self._braintrust_logger is not None:
            try:
                self._braintrust_logger.flush()
            except Exception:
                pass
        self._braintrust_logger = None
        self._braintrust_module = None
        self._enabled = False
        self._initialized = False

    def init(self, project: str, api_key: Optional[str] = None) -> bool:
        if self._initialized:
            if project != self._project:
                logger.warning(
                    f"Braintrust already initialized with project '{self._project}'. "
                    f"Ignoring request for project '{project}'. Use reset_instance() first."
                )
            return self._enabled

        api_key = api_key or os.getenv("BRAINTRUST_API_KEY")
        self._project = project

        if not api_key:
            logger.debug("BRAINTRUST_API_KEY not set. Braintrust logging disabled.")
            self._enabled = False
            self._initialized = True
            return False

        try:
            import braintrust
            self._braintrust_module = braintrust

            self._braintrust_logger = braintrust.init_logger(
                project=self._project,
                api_key=api_key
            )

            self._enabled = True
            self._initialized = True
            logger.info(f"Braintrust initialized for project: {self._project}")
            return True

        except ImportError:
            logger.warning("braintrust package not installed. Run: uv add braintrust")
            self._enabled = False
            self._initialized = True
            return False
        except Exception as e:
            logger.error(f"Failed to initialize Braintrust: {e}")
            self._enabled = False
            self._initialized = True
            return False

    def traced_call(
        self,
        name: str,
        input_data: Dict[str, Any],
        llm_call_fn: Callable[[], T],
        prompt: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        tags: Optional[list] = None
    ) -> T:
        if not self._enabled or self._braintrust_logger is None:
            return llm_call_fn()

        result = llm_call_fn()

        try:
            with self._braintrust_logger.start_span(name=name) as span:
                output_data = self._serialize_output(result)

                log_metadata = metadata.copy() if metadata else {}
                if prompt:
                    log_metadata["prompt"] = prompt

                span.log(
                    input=input_data,
                    output=output_data,
                    tags=tags or [],
                    metadata=log_metadata
                )
        except Exception as e:
            logger.warning(f"Braintrust logging failed: {e}")

        return result

    def _serialize_output(self, result: Any) -> Dict[str, Any]:
        if result is None:
            return {"result": None}

        if hasattr(result, 'model_dump'):
            try:
                return result.model_dump()
            except Exception:
                pass

        if isinstance(result, dict):
            return result

        if isinstance(result, (str, int, float, bool)):
            return {"result": result}

        if isinstance(result, (list, tuple)):
            return {"result": list(result)}

        return {"result": str(result)}

    def load_prompt(
        self,
        prompt_name: str,
        fallback_prompt: str,
        variables: Optional[Dict[str, Any]] = None
    ) -> str:
        if not self._enabled or self._braintrust_module is None:
            return self._render_prompt(fallback_prompt, variables)

        try:
            prompt = self._braintrust_module.load_prompt(
                project=self._project,
                slug=prompt_name
            )

            if prompt is None:
                logger.debug(f"Prompt '{prompt_name}' not found in Braintrust, using fallback")
                return self._render_prompt(fallback_prompt, variables)

            rendered = prompt.build(**(variables or {}))

            if isinstance(rendered, str):
                return rendered

            if hasattr(rendered, 'messages') and rendered.messages:
                contents = []
                for msg in rendered.messages:
                    if isinstance(msg, dict):
                        contents.append(msg.get('content', ''))
                    elif hasattr(msg, 'content'):
                        contents.append(str(msg.content))
                    else:
                        contents.append(str(msg))
                return "\n".join(contents)

            return str(rendered)

        except Exception as e:
            logger.warning(f"Failed to load prompt '{prompt_name}' from Braintrust: {e}")
            return self._render_prompt(fallback_prompt, variables)

    def _render_prompt(self, prompt: str, variables: Optional[Dict[str, Any]]) -> str:
        if not variables:
            return prompt

        try:
            return prompt.format(**variables)
        except KeyError as e:
            logger.warning(f"Prompt template missing variable: {e}")
            return prompt
        except ValueError as e:
            logger.warning(f"Prompt template format error: {e}")
            return prompt

    def flush(self) -> None:
        if self._braintrust_logger is not None:
            try:
                self._braintrust_logger.flush()
            except Exception as e:
                logger.error(f"Failed to flush Braintrust logs: {e}")

    def is_enabled(self) -> bool:
        return self._enabled

    def get_project(self) -> Optional[str]:
        return self._project


def init_braintrust(project: str, api_key: Optional[str] = None) -> bool:
    return BraintrustClient.get_instance().init(project, api_key)


def traced_llm_call(
    name: str,
    input_data: Dict[str, Any],
    llm_call_fn: Callable[[], T],
    prompt: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    tags: Optional[list] = None
) -> T:
    return BraintrustClient.get_instance().traced_call(
        name, input_data, llm_call_fn, prompt, metadata, tags
    )


def load_prompt_from_braintrust(
    prompt_name: str,
    fallback_prompt: str,
    variables: Optional[Dict[str, Any]] = None
) -> str:
    return BraintrustClient.get_instance().load_prompt(prompt_name, fallback_prompt, variables)


def flush_logs() -> None:
    BraintrustClient.get_instance().flush()


def is_enabled() -> bool:
    return BraintrustClient.get_instance().is_enabled()


def get_client() -> BraintrustClient:
    return BraintrustClient.get_instance()
