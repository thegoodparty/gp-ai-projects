"""Unit tests for the killed-run cost accumulator.

The timeout kill path never sees a ResultMessage, so cost is summed per-turn
from each AssistantMessage's usage. These lock down the pricing math (each
token class priced at its own rate, dollars summed across turns) and the
module-level accumulator helpers.
"""
from __future__ import annotations

from pmf_engine.runner.harness import claude_sdk


class TestPriceTurn:
    def test_prices_each_token_class_at_its_own_rate(self):
        # 1M input @ $3, 1M output @ $15, 1M cache-read @ $0.30, 1M cache-write @ $3.75
        usage = {
            "input_tokens": 1_000_000,
            "output_tokens": 1_000_000,
            "cache_read_input_tokens": 1_000_000,
            "cache_creation_input_tokens": 1_000_000,
        }
        assert claude_sdk._price_turn("claude-sonnet-5", usage) == 3.0 + 15.0 + 0.3 + 3.75

    def test_matches_bedrock_prefixed_model_string(self):
        usage = {"output_tokens": 1_000_000}
        assert claude_sdk._price_turn("anthropic.claude-sonnet-4-6", usage) == 15.0

    def test_opus_and_haiku_priced_distinctly(self):
        usage = {"output_tokens": 1_000_000}
        assert claude_sdk._price_turn("claude-opus-4-8", usage) == 25.0
        assert claude_sdk._price_turn("claude-haiku-4-5", usage) == 5.0

    def test_unknown_model_prices_zero(self):
        assert claude_sdk._price_turn("gemini-3-flash", {"output_tokens": 1_000_000}) == 0.0

    def test_missing_usage_fields_default_to_zero(self):
        assert claude_sdk._price_turn("claude-sonnet-5", {}) == 0.0
        assert claude_sdk._price_turn("claude-sonnet-5", None) == 0.0

    def test_cache_read_is_cheap_relative_to_fresh_input(self):
        # The core reason we sum per-turn dollars instead of summing input
        # tokens: a turn dominated by cache-reads costs ~10x less than the same
        # token count as fresh input.
        cached = claude_sdk._price_turn("claude-sonnet-5", {"cache_read_input_tokens": 1_000_000})
        fresh = claude_sdk._price_turn("claude-sonnet-5", {"input_tokens": 1_000_000})
        assert cached == 0.3 and fresh == 3.0


class TestAccumulator:
    def test_reset_zeroes_the_accumulator(self):
        claude_sdk._accumulated_cost_usd = 4.2
        claude_sdk.reset_accumulated_cost()
        assert claude_sdk.get_accumulated_cost() == 0.0

    def test_getter_reflects_current_state(self):
        claude_sdk.reset_accumulated_cost()
        claude_sdk._accumulated_cost_usd += claude_sdk._price_turn(
            "claude-sonnet-5", {"output_tokens": 2_000_000}
        )
        assert claude_sdk.get_accumulated_cost() == 30.0
