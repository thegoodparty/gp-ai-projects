"""
TDD red tests for research_atom — atomic research primitive.

Contract: a research_atom takes ONE question, fans through web/gov sources,
returns a verified fact with at least 2 cross-referenced sources (gov-preferred)
or `answer=None, confidence="unknown"` if it can't.

The load-bearing guardrail is quote verification: every cited source must
literally contain its quote (after aggressive normalization). Fabricated quotes
are dropped by the post-verifier; the atom cannot claim a high-confidence fact
from a source whose quote doesn't appear in the page body.
"""

from __future__ import annotations
import json
from unittest.mock import patch

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
)

from pmf_engine.research.research_atom import (
    research_atom,
    research_atom_with_retry,
    AtomResult,
)
from pmf_engine.research import verify


# ---------------------------------------------------------------------------
# verify.quote_in — the guardrail helper (used both by agent and post-verifier)
# ---------------------------------------------------------------------------


class TestVerifyQuoteIn:
    def test_exact_match_returns_match_true(self):
        body = "The fiscal year 2026 property tax rate is $0.5551 per $100 assessed value."
        quote = "property tax rate is $0.5551 per $100"
        result = verify.quote_in(body, quote)
        assert result["match"] is True
        assert result["similarity"] == 1.0

    def test_handles_whitespace_differences(self):
        body = "The fiscal  year\n2026 property tax\trate is $0.5551."
        quote = "fiscal year 2026 property tax rate is $0.5551"
        assert verify.quote_in(body, quote)["match"] is True

    def test_handles_case_differences(self):
        body = "THE CITY OF DURHAM adopted its FY2026 BUDGET on June 17, 2025."
        quote = "the city of durham adopted its fy2026 budget"
        assert verify.quote_in(body, quote)["match"] is True

    def test_aggressive_handles_unicode_dashes_and_quotes(self):
        body = "Resolution \u201cSupport Transit\u201d \u2014 adopted 7\u20130."
        quote = 'Resolution "Support Transit" - adopted 7-0'
        assert verify.quote_in(body, quote, aggressive=True)["match"] is True

    def test_fabricated_quote_returns_match_false(self):
        body = "The fiscal year 2026 property tax rate is $0.5551 per $100."
        quote = "The mayor announced a $50 million stadium project"
        result = verify.quote_in(body, quote)
        assert result["match"] is False

    def test_returns_closest_match_and_similarity_on_miss(self):
        body = "The fiscal year 2026 property tax rate is $0.5551 per $100 assessed value."
        quote = "property tax rate is $0.5720 per $100"  # wrong number, same structure
        result = verify.quote_in(body, quote)
        assert result["match"] is False
        assert result["closest_match"] is not None
        assert "property tax rate" in result["closest_match"]
        assert 0.5 < result["similarity"] < 1.0  # partial overlap

    def test_empty_quote_is_never_a_match(self):
        assert verify.quote_in("any body", "")["match"] is False

    def test_aggressive_strips_html_tags(self):
        """Raw HTML with inline markup; agent's quote is plain text. Must match."""
        body = "<p>The <strong>FY2026</strong> property tax rate is <em>$0.5551</em> per $100.</p>"
        quote = "FY2026 property tax rate is $0.5551 per $100"
        assert verify.quote_in(body, quote, aggressive=True)["match"] is True

    def test_aggressive_decodes_html_entities(self):
        """HTML-entity-encoded chars should decode before comparison."""
        body = "Support Transit &mdash; adopted 7&ndash;0 by a vote of 7&nbsp;yes, 0&nbsp;no."
        quote = 'Support Transit - adopted 7-0 by a vote of 7 yes, 0 no'
        assert verify.quote_in(body, quote, aggressive=True)["match"] is True

    def test_aggressive_strips_script_and_style_content(self):
        """<script> and <style> blocks often contain text that fuzzy-matches
        unrelated quotes. They should be stripped, not just their tags."""
        body = (
            "<style>body { font-size: 14px; }</style>"
            "<script>var tax_rate = 'fake $0.9999';</script>"
            "<p>Real content: FY2026 tax rate is $0.5551.</p>"
        )
        quote = "FY2026 tax rate is $0.5551"
        assert verify.quote_in(body, quote, aggressive=True)["match"] is True

    def test_aggressive_handles_text_split_across_tags(self):
        """A phrase that spans HTML tags in the raw source (common in real pages)
        should still match after tag stripping."""
        body = "The <span>proposed</span> tax <a href='/x'>rate</a> of 43.71 cents"
        quote = "proposed tax rate of 43.71 cents"
        assert verify.quote_in(body, quote, aggressive=True)["match"] is True

    def test_strict_mode_does_not_strip_html(self):
        """Strict mode preserves exact-byte semantics. A quote that's split
        across inline HTML tags in raw source will NOT match in strict mode
        (the `</span>` remains between 'proposed' and 'tax'). This is the
        intended opt-out for callers who want exact-text verification
        (e.g. against pdftotext output)."""
        body = "The <span>proposed</span> tax rate of 43.71 cents"
        quote = "proposed tax rate of 43.71 cents"
        # Aggressive strips tags → match
        assert verify.quote_in(body, quote, aggressive=True)["match"] is True
        # Strict leaves tags in place → no match (quote is split by </span>)
        assert verify.quote_in(body, quote, aggressive=False)["match"] is False


# ---------------------------------------------------------------------------
# research_atom — the fan-out unit
# ---------------------------------------------------------------------------


def _make_query(json_answer: str | dict, cost: float = 0.02, turns: int = 4):
    """Build a fake claude_agent_sdk.query that returns a single assistant message
    containing the JSON answer, then a ResultMessage."""
    payload = json_answer if isinstance(json_answer, str) else json.dumps(json_answer)

    async def _fake(prompt, options):
        yield AssistantMessage(
            model="sonnet",
            content=[TextBlock(text=payload)],
        )
        yield ResultMessage(
            subtype="result",
            duration_ms=1000,
            duration_api_ms=900,
            is_error=False,
            num_turns=turns,
            session_id="atom-test",
            total_cost_usd=cost,
            result=payload,
        )

    return _fake


def _canned_fetch(url_to_body: dict[str, str]):
    """Fake fetch function: returns canned body for each URL, raises on unknown URL."""

    async def _fake(url: str) -> str:
        if url not in url_to_body:
            raise KeyError(f"unexpected URL fetched in verify phase: {url}")
        return url_to_body[url]

    return _fake


@pytest.mark.asyncio
async def test_atom_high_confidence_requires_gov_plus_two_verified():
    """Happy path: 2 verified sources, at least one .gov → confidence=high."""
    atom_json = {
        "question": "Durham NC property tax rate FY 2026",
        "answer": "$0.5551 per $100 assessed value",
        "summary": "Durham's FY2026 property tax rate is $0.5551 per $100, unchanged from FY2025 [s1][s2].",
        "sources": [
            {
                "url": "https://durhamnc.gov/tax-rate",
                "type": "government_record",
                "quote": "The FY2026 property tax rate is $0.5551 per $100",
            },
            {
                "url": "https://linc.nc.gov/durham-tax",
                "type": "government_record",
                "quote": "Durham property tax rate for fiscal year 2026: $0.5551",
            },
        ],
        "disagreements": [],
        "reasoning_trace": "Cross-referenced Durham govt site and NC LINC; both agree.",
    }
    bodies = {
        "https://durhamnc.gov/tax-rate":
            "Durham Budget Office. The FY2026 property tax rate is $0.5551 per $100 of assessed value.",
        "https://linc.nc.gov/durham-tax":
            "NC State Data Portal. Durham property tax rate for fiscal year 2026: $0.5551.",
    }

    with (
        patch("pmf_engine.research.research_atom.query", side_effect=_make_query(atom_json)),
        patch("pmf_engine.research.research_atom._fetch_for_verify", side_effect=_canned_fetch(bodies)),
    ):
        result = await research_atom("Durham NC property tax rate FY 2026")

    assert result["confidence"] == "high"
    assert result["answer"] == "$0.5551 per $100 assessed value"
    assert all(s["verified"] for s in result["sources"])


@pytest.mark.asyncio
async def test_atom_drops_fabricated_quote_in_post_verify():
    """The load-bearing test: agent claims a quote that doesn't appear in body.
    Post-verifier must mark verified=False; drop it from min_sources tally."""
    atom_json = {
        "question": "Durham NC property tax rate FY 2026",
        "answer": "$0.5551 per $100",
        "summary": "Durham's rate is $0.5551 [s1][s2].",
        "sources": [
            {
                "url": "https://durhamnc.gov/tax-rate",
                "type": "government_record",
                "quote": "The FY2026 property tax rate is $0.5551 per $100",
            },
            {
                "url": "https://example.com/news",
                "type": "news",
                "quote": "Durham's mayor announced a 300% property tax increase",  # fabricated
            },
        ],
        "disagreements": [],
        "reasoning_trace": "",
    }
    bodies = {
        "https://durhamnc.gov/tax-rate":
            "Durham Budget Office. The FY2026 property tax rate is $0.5551 per $100.",
        "https://example.com/news":
            "Durham news: new library branch opens downtown this weekend.",  # no mention of mayor/tax
    }

    with (
        patch("pmf_engine.research.research_atom.query", side_effect=_make_query(atom_json)),
        patch("pmf_engine.research.research_atom._fetch_for_verify", side_effect=_canned_fetch(bodies)),
    ):
        result = await research_atom("Durham NC property tax rate FY 2026")

    # Gov source verifies, fabricated source gets verified=False
    gov = next(s for s in result["sources"] if s["url"].endswith("tax-rate"))
    fake = next(s for s in result["sources"] if s["url"].endswith("news"))
    assert gov["verified"] is True
    assert fake["verified"] is False

    # Only 1 verified source → confidence low, not high
    assert result["confidence"] == "low"


@pytest.mark.asyncio
async def test_atom_unknown_when_zero_verified_sources():
    """All quotes fabricated → answer=None, confidence=unknown."""
    atom_json = {
        "question": "Q",
        "answer": "some guess",
        "summary": "guessing",
        "sources": [
            {"url": "https://a.gov/x", "type": "government_record", "quote": "ghost A"},
            {"url": "https://b.gov/y", "type": "government_record", "quote": "ghost B"},
        ],
        "disagreements": [],
        "reasoning_trace": "",
    }
    bodies = {
        "https://a.gov/x": "completely unrelated content A",
        "https://b.gov/y": "completely unrelated content B",
    }

    with (
        patch("pmf_engine.research.research_atom.query", side_effect=_make_query(atom_json)),
        patch("pmf_engine.research.research_atom._fetch_for_verify", side_effect=_canned_fetch(bodies)),
    ):
        result = await research_atom("Q")

    assert result["answer"] is None
    assert result["confidence"] == "unknown"
    assert all(s["verified"] is False for s in result["sources"])


@pytest.mark.asyncio
async def test_atom_medium_when_two_verified_but_no_gov():
    """2 verified but no government source → medium confidence."""
    atom_json = {
        "question": "Q",
        "answer": "A",
        "summary": "Context [s1][s2].",
        "sources": [
            {"url": "https://npr.org/x", "type": "news", "quote": "fact one"},
            {"url": "https://nyt.com/y", "type": "news", "quote": "fact two"},
        ],
        "disagreements": [],
        "reasoning_trace": "",
    }
    bodies = {
        "https://npr.org/x": "NPR headline: fact one confirmed by two officials.",
        "https://nyt.com/y": "NYT article says fact two, according to reporting.",
    }

    with (
        patch("pmf_engine.research.research_atom.query", side_effect=_make_query(atom_json)),
        patch("pmf_engine.research.research_atom._fetch_for_verify", side_effect=_canned_fetch(bodies)),
    ):
        result = await research_atom("Q")

    assert result["confidence"] == "medium"


@pytest.mark.asyncio
async def test_atom_low_when_only_one_verified():
    atom_json = {
        "question": "Q",
        "answer": "A",
        "summary": "only one source [s1].",
        "sources": [
            {"url": "https://a.gov/x", "type": "government_record", "quote": "real fact"},
            {"url": "https://b.com/y", "type": "news", "quote": "ghost fact"},
        ],
        "disagreements": [],
        "reasoning_trace": "",
    }
    bodies = {
        "https://a.gov/x": "Official report: real fact disclosed in press release.",
        "https://b.com/y": "completely unrelated blog post",
    }

    with (
        patch("pmf_engine.research.research_atom.query", side_effect=_make_query(atom_json)),
        patch("pmf_engine.research.research_atom._fetch_for_verify", side_effect=_canned_fetch(bodies)),
    ):
        result = await research_atom("Q")

    assert result["confidence"] == "low"
    assert result["answer"] == "A"  # kept at low confidence, flagged


@pytest.mark.asyncio
async def test_atom_summary_strips_citations_for_unverified_sources():
    """If summary references [s2] but source 2 fails verification, the reference
    should be flagged in reasoning_trace (summary is kept but orchestrator is warned)."""
    atom_json = {
        "question": "Q",
        "answer": "A",
        "summary": "Gov says X [s1], and another source confirms Y [s2].",
        "sources": [
            {"url": "https://a.gov/x", "type": "government_record", "quote": "real one"},
            {"url": "https://b.com/y", "type": "news", "quote": "ghost"},
        ],
        "disagreements": [],
        "reasoning_trace": "",
    }
    bodies = {
        "https://a.gov/x": "real one appears here",
        "https://b.com/y": "no matching quote anywhere",
    }

    with (
        patch("pmf_engine.research.research_atom.query", side_effect=_make_query(atom_json)),
        patch("pmf_engine.research.research_atom._fetch_for_verify", side_effect=_canned_fetch(bodies)),
    ):
        result = await research_atom("Q")

    # trace must call out the unverified citation
    assert "s2" in (result["reasoning_trace"] or "").lower() or \
           "unverified" in (result["reasoning_trace"] or "").lower()


@pytest.mark.asyncio
async def test_atom_records_cost_and_turns():
    atom_json = {
        "question": "Q",
        "answer": "A",
        "summary": "x [s1][s2]",
        "sources": [
            {"url": "https://a.gov/x", "type": "government_record", "quote": "a"},
            {"url": "https://b.gov/y", "type": "government_record", "quote": "b"},
        ],
        "disagreements": [],
        "reasoning_trace": "",
    }
    bodies = {"https://a.gov/x": "a in body", "https://b.gov/y": "b in body"}

    with (
        patch(
            "pmf_engine.research.research_atom.query",
            side_effect=_make_query(atom_json, cost=0.07, turns=6),
        ),
        patch("pmf_engine.research.research_atom._fetch_for_verify", side_effect=_canned_fetch(bodies)),
    ):
        result = await research_atom("Q")

    assert result["cost_usd"] == 0.07
    assert result["turns_used"] == 6


# ---------------------------------------------------------------------------
# research_atom_with_retry — thin retry on unknown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_succeeds_on_second_try_after_unknown():
    """First atom call returns unknown; retry rephrases and succeeds."""
    first_call_json = {
        "question": "Q",
        "answer": "guess",
        "summary": "unsure",
        "sources": [{"url": "https://a.gov/x", "type": "government_record", "quote": "ghost"}],
        "disagreements": [],
        "reasoning_trace": "",
    }
    second_call_json = {
        "question": "Q (rephrased)",
        "answer": "A",
        "summary": "confirmed [s1][s2]",
        "sources": [
            {"url": "https://a.gov/x", "type": "government_record", "quote": "real one"},
            {"url": "https://b.gov/y", "type": "government_record", "quote": "real two"},
        ],
        "disagreements": [],
        "reasoning_trace": "",
    }
    bodies = {
        "https://a.gov/x": "real one is here",
        "https://b.gov/y": "real two is here",
    }

    calls = []

    async def fake_query(prompt, options):
        calls.append(prompt)
        payload = first_call_json if len(calls) == 1 else second_call_json
        async for msg in _make_query(payload)(prompt, options):
            yield msg

    with (
        patch("pmf_engine.research.research_atom.query", side_effect=fake_query),
        patch("pmf_engine.research.research_atom._fetch_for_verify", side_effect=_canned_fetch(bodies)),
    ):
        result = await research_atom_with_retry("Q", max_retries=1)

    assert result["confidence"] == "high"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_retry_accepts_unknown_after_retry_limit():
    """Both attempts return unknown → final result is unknown (honest null)."""
    atom_json = {
        "question": "Q",
        "answer": "g",
        "summary": "sure",
        "sources": [{"url": "https://a.gov/x", "type": "government_record", "quote": "ghost"}],
        "disagreements": [],
        "reasoning_trace": "",
    }
    bodies = {"https://a.gov/x": "totally unrelated"}

    calls = []

    async def fake_query(prompt, options):
        calls.append(prompt)
        async for msg in _make_query(atom_json)(prompt, options):
            yield msg

    with (
        patch("pmf_engine.research.research_atom.query", side_effect=fake_query),
        patch("pmf_engine.research.research_atom._fetch_for_verify", side_effect=_canned_fetch(bodies)),
    ):
        result = await research_atom_with_retry("Q", max_retries=1)

    assert result["confidence"] == "unknown"
    assert result["answer"] is None
    assert len(calls) == 2  # original + 1 retry
