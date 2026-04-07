"""Tests for prompt builders in prompts/"""
import sys
from pathlib import Path
import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from meeting_pipeline.prompts.extraction import build_extraction_prompt
from meeting_pipeline.prompts.briefing import (
    EDITORIAL_RULES,
    build_pass1_prompt,
    build_pass2_prompt,
    build_pass3_prompt,
)


def test_extraction_prompt_contains_city():
    prompt = build_extraction_prompt("sample text", "Durham", "NC", "2026-04-07")
    assert "Durham" in prompt
    assert "NC" in prompt
    assert "2026-04-07" in prompt


def test_extraction_prompt_large_agenda_uses_short_description():
    short = build_extraction_prompt("x", "Durham", "NC", "2026-04-07", large_agenda=False)
    large = build_extraction_prompt("x", "Durham", "NC", "2026-04-07", large_agenda=True)
    assert "1-3 sentence" in short
    assert "1-sentence" in large


def test_extraction_prompt_truncates_text():
    long_text = "word " * 20_000  # 100K chars
    prompt = build_extraction_prompt(long_text, "Durham", "NC", "2026-04-07")
    assert len(prompt) < 200_000  # truncated to 50K chars of text


def test_editorial_rules_not_empty():
    assert len(EDITORIAL_RULES) > 100
    assert "NEVER" in EDITORIAL_RULES
    assert "vote" in EDITORIAL_RULES.lower()


def test_pass1_prompt_contains_city():
    prompt = build_pass1_prompt("Durham", "City Council", "2026-04-07", "item text")
    assert "Durham" in prompt
    assert "City Council" in prompt


def test_pass1_prompt_includes_constituent_context():
    prompt = build_pass1_prompt(
        "Durham", "City Council", "2026-04-07", "items",
        constituent_context="Public Safety: 77/100"
    )
    assert "Public Safety" in prompt


def test_pass2_prompt_contains_editorial_rules():
    prompt = build_pass2_prompt(
        "Durham", "City Council", "2026-04-07", "Monday",
        "items text", "Summary sentence.", 10
    )
    assert "EDITORIAL RULES" in prompt


def test_pass3_prompt_contains_grounding_rule():
    prompt = build_pass3_prompt(
        "Durham", "NC", "City Council", "2026-04-07", "Monday",
        "Budget Amendment", "vote_required", "Description.", "Large item",
        "Card headline", "Source text here", "Other items"
    )
    assert "GROUNDING RULE" in prompt
    assert "Budget Amendment" in prompt
