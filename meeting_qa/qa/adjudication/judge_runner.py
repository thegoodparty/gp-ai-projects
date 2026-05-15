"""judge_runner.py — LLM provider dispatch with retry logic.

Supported providers: anthropic, google, openai.
Adding a provider: implement _run_<provider> and add it to _RUNNERS.
"""
from __future__ import annotations

import json
import re
import time


# ── JSON parsing ──────────────────────────────────────────────────────────────

def _parse_json(text: str) -> dict:
    """Extract a JSON object from model output. Handles code fences and embedded JSON."""
    def _try(s: str) -> dict | None:
        m = re.search(r"```(?:json)?\s*(.*?)\s*```", s, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass
        try:
            return json.loads(s.strip())
        except json.JSONDecodeError:
            pass
        m = re.search(r"\{.*\}", s.strip(), re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
        return None

    result = _try(text)
    if result is not None:
        return result
    # Models sometimes emit \' (invalid in JSON) — strip and retry
    result = _try(text.replace("\\'", "'"))
    if result is not None:
        return result
    raise json.JSONDecodeError(
        f"No valid JSON found (preview={text[:150]!r})", text, 0
    )


# ── Per-provider runners ──────────────────────────────────────────────────────

def _run_anthropic(prompt: str, api_key: str, model: str, max_tokens: int) -> dict:
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    msg = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
        timeout=60.0,
    )
    return _parse_json(msg.content[0].text)


def _run_google(prompt: str, api_key: str, model: str, max_tokens: int) -> dict:
    from google import genai
    from google.genai import types as genai_types
    client = genai.Client(api_key=api_key)
    resp = client.models.generate_content(
        model=model,
        contents=prompt,
        config=genai_types.GenerateContentConfig(
            max_output_tokens=max_tokens,
            thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
        ),
    )
    if not resp.text:
        reason = str(resp.candidates[0].finish_reason) if resp.candidates else "none"
        raise RuntimeError(f"503 UNAVAILABLE — Gemini empty response (finish_reason={reason})")
    return _parse_json(resp.text)


def _run_openai(prompt: str, api_key: str, model: str, max_tokens: int) -> dict:
    from openai import OpenAI
    client = OpenAI(api_key=api_key)
    resp = client.chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        response_format={"type": "json_object"},
        messages=[{"role": "user", "content": prompt}],
    )
    return json.loads(resp.choices[0].message.content)


_RUNNERS = {
    "anthropic": _run_anthropic,
    "google":    _run_google,
    "openai":    _run_openai,
}


# ── Dispatch ──────────────────────────────────────────────────────────────────

def dispatch(
    provider: str,
    model: str,
    api_key: str,
    prompt: str,
    max_tokens: int = 512,
    retries: int = 3,
    backoff: float = 8.0,
) -> dict:
    """Dispatch a prompt to the specified provider with retry on transient errors."""
    runner = _RUNNERS.get(provider)
    if runner is None:
        raise ValueError(f"Unknown provider: {provider!r}. Supported: {list(_RUNNERS)}")
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            return runner(prompt, api_key, model, max_tokens)
        except Exception as e:
            msg = str(e)
            if "503" in msg or "UNAVAILABLE" in msg or "overloaded" in msg.lower() or "timeout" in msg.lower() or "timed out" in msg.lower():
                last_exc = e
                if attempt < retries - 1:
                    time.sleep(backoff * (attempt + 1))
                continue
            raise
    raise last_exc  # type: ignore[misc]
