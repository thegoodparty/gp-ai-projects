"""
research_atom — atomic research primitive.

Answers ONE specific question with ≥2 cross-referenced sources (gov-preferred).
Every cited quote is literally verified against its page body. Fabrications
are dropped; insufficient evidence returns {answer=None, confidence="unknown"}.

The shape is intentionally small:
- Pure function (no workspace coupling, no side effects)
- Deterministic confidence resolution (no LLM judgment over confidence)
- Defense-in-depth verification: the atom self-verifies during its turns
  (via the `verify.quote_in` helper), and this harness re-verifies against
  a canonical fetched body after the atom returns.

Composition: the orchestrator fans out N atoms in parallel (asyncio.gather
with a semaphore to stay under Anthropic concurrency limits). Only results
with confidence in {high, medium} are used for hard facts; summary prose is
labeled analysis, not fact.
"""

from __future__ import annotations
import json
import re
from typing import Literal, TypedDict

from claude_agent_sdk import (
    query,
    ClaudeAgentOptions,
    AssistantMessage,
    TextBlock,
    ResultMessage,
)

from pmf_engine.research import verify
from shared.logger import get_logger

logger = get_logger(__name__)


class Source(TypedDict):
    url: str
    type: Literal["government_record", "staff_report", "news", "web", "campaign"]
    quote: str
    fetched_at: str
    verified: bool


class AtomResult(TypedDict):
    question: str
    answer: str | None
    summary: str | None
    confidence: Literal["high", "medium", "low", "unknown"]
    sources: list[Source]
    disagreements: list[str]
    turns_used: int
    cost_usd: float
    reasoning_trace: str


_ALLOWED_TOOLS = ["Bash", "WebSearch", "WebFetch"]  # read-only; no Write/Edit

_ATOM_SYSTEM_PROMPT = """You are a research atom. You answer ONE specific factual question.

You have limited turns. Research efficiently, then STOP and emit JSON.

## Workflow

1. **Search** — 1-2 WebSearch calls to locate candidate sources. Prefer `.gov`,
   state `.us`, staff reports, official budgets over news over blogs.
2. **Fetch** — WebFetch the top 2-3 candidate pages. For each, note the URL
   and a literal 5-50 word quote that directly supports your answer.
3. **Self-check quotes** — before emitting JSON, for each source you plan to
   cite, scroll back through the fetched page text and find your quote
   verbatim. If you can't find the exact phrase, SHORTEN the quote to a
   segment you CAN locate word-for-word, or DROP the source. Never
   paraphrase — quotes must be copy-pasted from what you saw.
4. **Emit JSON** — once you have 2 cross-referencing, self-checked quotes
   (ideally 1+ from `.gov`), STOP researching. Emit the JSON below. Do not
   do more searches.

## Field meanings (strict)

- `answer` — the single verified fact you claim as truth. Must be directly
  supported by at least one entry in `sources[]`. Under 200 chars.
- `source.quote` — **literal** text from the fetched page. This is the
  guardrail: a post-verifier re-fetches each URL and checks the quote appears
  verbatim. Paraphrased quotes are dropped. Better to cite a shorter exact
  phrase than a longer approximation.
- `summary` — 1-3 sentences of **prose synthesis** tying sources together,
  with `[s1][s2]` inline citations. Synthesis is allowed here — the summary
  is analysis, not a verified quote, and does NOT need to be literally
  verifiable. Treat it like a chief-of-staff's note.
- `sources[]` — the evidence base. Every source should have a verified quote
  that supports the answer or the summary.

## Emitting JSON — the only acceptable final message

Your LAST message must be exactly one JSON object in this schema. No prose
before or after. No markdown code fences. Just the JSON:

```
{
  "question": "<the question verbatim>",
  "answer": "<the verified fact, under 200 chars, or null>",
  "summary": "<1-3 sentences with [s1][s2] inline citations, or null>",
  "sources": [
    {
      "url": "<full URL>",
      "type": "government_record|staff_report|news|web|campaign",
      "quote": "<literal text copied from the fetched page, 5-50 words>"
    }
  ],
  "disagreements": ["<any source conflicts, or empty array>"],
  "reasoning_trace": "<1-2 sentences on how you reached the answer>"
}
```

## Hard rules

- **Every `source.quote` MUST appear verbatim in the fetched page.** If a
  page doesn't contain the exact text you want, either pick different text
  from that page or drop the source. Paraphrasing will be caught by the
  post-verifier and the source will be dropped.
- **`summary` is prose and may synthesize** — but every factual claim in
  summary must trace to a numbered source. Do not introduce facts not
  present in your sources.
- **If you can't find ≥2 supporting sources after ~6 turns of searching,
  emit JSON with `answer: null` and `sources: []` (or partial sources).**
  Honest null is better than fabrication.
- **When you are on your second-to-last turn, STOP RESEARCHING and emit JSON
  with whatever you have.** A partial result is better than no result.
"""


async def _fetch_for_verify(url: str) -> str:
    """Canonical body fetch for post-verification. Monkeypatched in tests.

    At runtime this uses the broker-backed pmf_runtime.http.get via a thread so
    the sync httpx.Client never blocks the caller's event loop. Critical when
    the broker runs in-process on the same loop (local dev / integration tests),
    where blocking would deadlock — and harmless otherwise."""
    import asyncio
    from pmf_engine.runner.pmf_runtime import http

    r = await asyncio.to_thread(http.get, url, "atom post-verify")
    return r.get("body", "")


def _parse_atom_json(text: str) -> dict:
    """Extract the final JSON payload from the atom's last assistant message.

    Tolerates: bare JSON, JSON inside ```json fences, JSON with prefixing prose.
    """
    # Try ```json fence first
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        return json.loads(fence.group(1))
    # Bare JSON — find first '{' and matching '}'
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object found in atom response")
    # Walk braces to find matching close
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start : i + 1])
    raise ValueError("unbalanced JSON in atom response")


async def _run_atom_query(question: str, model: str, max_turns: int) -> tuple[dict, float, int]:
    """Run the nested Claude agent, return (parsed_json, cost_usd, turns_used).

    The agent may interleave tool-use turns with text turns. We collect *all*
    assistant text blocks, then prefer parseable JSON in this priority order:
      1. ResultMessage.result (SDK's canonical final response)
      2. Last assistant text block that contains a `{...}` payload
      3. Concatenation of all assistant text blocks
    """
    options = ClaudeAgentOptions(
        system_prompt=_ATOM_SYSTEM_PROMPT,
        allowed_tools=_ALLOWED_TOOLS,
        permission_mode="bypassPermissions",
        max_turns=max_turns,
        model=model,
    )

    prompt = f"Research this question and return the JSON schema described in your instructions.\n\nQuestion: {question}"

    text_blocks: list[str] = []
    result_text: str | None = None
    cost_usd = 0.0
    turns_used = 0

    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    text_blocks.append(block.text)
        elif isinstance(message, ResultMessage):
            cost_usd = message.total_cost_usd or 0.0
            turns_used = message.num_turns
            if message.is_error:
                raise RuntimeError(f"Atom agent error: {message.result}")
            result_text = message.result

    candidates: list[str] = []
    if result_text:
        candidates.append(result_text)
    for t in reversed(text_blocks):  # most-recent first
        if "{" in t and "}" in t:
            candidates.append(t)
    candidates.append("\n".join(text_blocks))

    parsed: dict | None = None
    last_err: Exception | None = None
    for c in candidates:
        try:
            parsed = _parse_atom_json(c)
            break
        except (ValueError, json.JSONDecodeError) as e:
            last_err = e

    if parsed is None:
        preview = (result_text or "\n".join(text_blocks))[:800]
        raise RuntimeError(
            f"Atom returned no parseable JSON after {turns_used} turns. "
            f"Last parse error: {last_err}. Text preview:\n{preview}"
        )

    return parsed, cost_usd, turns_used


async def _verify_sources(sources: list[dict]) -> list[Source]:
    """Re-fetch each source URL and verify quote literally appears. Defense-in-depth
    against the atom falsely reporting a successful self-verification."""
    from datetime import datetime, timezone

    verified: list[Source] = []
    now = datetime.now(timezone.utc).isoformat()
    for raw in sources:
        url = raw.get("url", "")
        quote = raw.get("quote", "")
        try:
            body = await _fetch_for_verify(url)
            match = verify.quote_in(body, quote, aggressive=True)["match"]
        except Exception as e:  # noqa: BLE001 — fetch failure = unverifiable
            logger.warning(f"Atom verify re-fetch failed for {url}: {e}")
            match = False
        verified.append({
            "url": url,
            "type": raw.get("type", "web"),
            "quote": quote,
            "fetched_at": raw.get("fetched_at", now),
            "verified": bool(match),
        })
    return verified


def _resolve_confidence(sources: list[Source], disagreements: list[str]) -> tuple[str, bool]:
    """Deterministic confidence resolution. Returns (confidence, keep_answer).

    Rules:
      ≥2 verified, ≥1 government → high
      ≥2 verified, non-gov       → medium
      1 verified                 → low (answer kept, flagged)
      0 verified                 → unknown (answer dropped)
      Any disagreement → downgrade one tier (high→medium, medium→low).
    """
    verified = [s for s in sources if s["verified"]]
    has_gov = any(s["type"] == "government_record" for s in verified)

    if len(verified) >= 2 and has_gov:
        level = "high"
    elif len(verified) >= 2:
        level = "medium"
    elif len(verified) == 1:
        level = "low"
    else:
        level = "unknown"

    if disagreements:
        level = {"high": "medium", "medium": "low", "low": "unknown", "unknown": "unknown"}[level]

    return level, level != "unknown"


def _annotate_trace(base: str, sources: list[Source]) -> str:
    unverified_idx = [i + 1 for i, s in enumerate(sources) if not s["verified"]]
    if unverified_idx:
        tags = ", ".join(f"s{i}" for i in unverified_idx)
        suffix = f" [post-verify: unverified sources dropped: {tags}]"
        return (base or "") + suffix
    return base or ""


async def research_atom(
    question: str,
    context: dict | None = None,
    prefer_types: list[str] | None = None,
    min_sources: int = 2,
    max_turns: int = 10,
    model: str = "sonnet",
) -> AtomResult:
    """Research one fact. Returns AtomResult with verified sources.

    Safe for asyncio.gather() fan-out — pure function, no shared state.
    """
    prefer_types = prefer_types or ["government_record"]
    logger.info(f"Atom: {question!r} (model={model}, max_turns={max_turns})")

    parsed, cost_usd, turns_used = await _run_atom_query(question, model, max_turns)

    raw_sources = parsed.get("sources") or []
    verified_sources = await _verify_sources(raw_sources)

    disagreements = list(parsed.get("disagreements") or [])
    confidence, keep_answer = _resolve_confidence(verified_sources, disagreements)

    answer = parsed.get("answer") if keep_answer else None
    summary = parsed.get("summary")
    trace = _annotate_trace(parsed.get("reasoning_trace", ""), verified_sources)

    return AtomResult(
        question=question,
        answer=answer,
        summary=summary,
        confidence=confidence,
        sources=verified_sources,
        disagreements=disagreements,
        turns_used=turns_used,
        cost_usd=cost_usd,
        reasoning_trace=trace,
    )


async def research_atom_with_retry(
    question: str,
    max_retries: int = 1,
    **atom_kwargs,
) -> AtomResult:
    """Retry once on `unknown`, rephrasing the question to give the model a
    fresh angle. Accepts unknown after the retry — honest null over fabrication."""
    result = await research_atom(question, **atom_kwargs)
    if result["confidence"] != "unknown" or max_retries <= 0:
        return result

    for attempt in range(max_retries):
        rephrased = f"{question} (prior attempt returned no verified sources; try official government portals, CAFR/budget documents, or primary-source news wire reports)"
        logger.info(f"Atom retry {attempt + 1}/{max_retries}: {rephrased!r}")
        result = await research_atom(rephrased, **atom_kwargs)
        if result["confidence"] != "unknown":
            return result

    return result
