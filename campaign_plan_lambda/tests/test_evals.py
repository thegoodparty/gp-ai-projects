"""Unit tests for campaign_plan_lambda/evals.py.

Three flavors of test:
- Pure-function scorer tests: exhaustive boundary/edge coverage for the
  four scorers (`count_in_range`, `dates_in_range`, `urls_valid`,
  `title_overlap`).
- `pipeline_task` short-circuit tests: locks the explicit branches that
  return an empty result on missing/malformed electionDate. The rest of
  pipeline_task is thin orchestration (assemble vars → render prompts →
  call helpers) and is integration-tested by PMs running real evals.
- `EVAL_PARAMETERS` shape tests: regression coverage for the
  null-field-in-prompt-block bug that hid our sandbox from the playground
  "+ Task → Remote eval" picker. See `TestEvalParametersShape` below."""

from datetime import date, timedelta
from unittest.mock import patch

import pytest


def _import_scorers():
    """Import the scorer functions without triggering the module-level
    `Eval(...)` call (which connects to Braintrust). Done with a stub for
    `braintrust.Eval` and `braintrust.init_dataset`."""
    import importlib
    import sys

    # If evals was already imported elsewhere (e.g. via push_eval_to_braintrust.sh), get
    # the existing module — Eval already ran but that's harmless for our
    # function references.
    if "campaign_plan_lambda.evals" in sys.modules:
        return sys.modules["campaign_plan_lambda.evals"]

    with patch("braintrust.Eval"), patch("braintrust.init_dataset"):
        return importlib.import_module("campaign_plan_lambda.evals")


evals = _import_scorers()


class TestCountInRange:
    def test_target_band_scores_one(self):
        for n in (5, 6, 7, 8):
            output = {"tasks": [{}] * n}
            assert evals.count_in_range(None, output) == 1.0

    def test_zero_scores_zero(self):
        assert evals.count_in_range(None, {"tasks": []}) == 0.0

    def test_below_band_scales_linearly(self):
        # 4 events / target floor of 5 = 0.8
        assert evals.count_in_range(None, {"tasks": [{}] * 4}) == 0.8

    def test_far_below_band(self):
        assert evals.count_in_range(None, {"tasks": [{}]}) == 0.2

    def test_above_band_scales_linearly(self):
        # n=12 → 1 - (12-8)/8 = 0.5
        assert evals.count_in_range(None, {"tasks": [{}] * 12}) == 0.5

    def test_far_above_band_clamps_to_zero(self):
        assert evals.count_in_range(None, {"tasks": [{}] * 100}) == 0.0

    def test_missing_tasks_field(self):
        assert evals.count_in_range(None, {}) == 0.0


class TestDatesInRange:
    def setup_method(self):
        # Election 60 days from today; today is "now" inside the scorer.
        self.today = date.today()
        self.election = self.today + timedelta(days=60)
        self.input = {"electionDate": self.election.isoformat()}

    def test_all_in_range_scores_one(self):
        output = {"tasks": [
            {"date": (self.today + timedelta(days=10)).isoformat()},
            {"date": (self.today + timedelta(days=30)).isoformat()},
        ]}
        assert evals.dates_in_range(self.input, output) == 1.0

    def test_past_event_excluded(self):
        # Lower bound (today) was added in this branch — past events used
        # to score as in-range.
        output = {"tasks": [
            {"date": (self.today - timedelta(days=1)).isoformat()},
            {"date": (self.today + timedelta(days=10)).isoformat()},
        ]}
        assert evals.dates_in_range(self.input, output) == 0.5

    def test_post_election_event_excluded(self):
        output = {"tasks": [
            {"date": (self.election + timedelta(days=1)).isoformat()},
            {"date": (self.today + timedelta(days=10)).isoformat()},
        ]}
        assert evals.dates_in_range(self.input, output) == 0.5

    def test_today_and_election_inclusive(self):
        output = {"tasks": [
            {"date": self.today.isoformat()},
            {"date": self.election.isoformat()},
        ]}
        assert evals.dates_in_range(self.input, output) == 1.0

    def test_invalid_date_string_dropped(self):
        output = {"tasks": [
            {"date": "not-a-date"},
            {"date": (self.today + timedelta(days=10)).isoformat()},
        ]}
        # 1 valid out of 2 total
        assert evals.dates_in_range(self.input, output) == 0.5

    def test_non_dict_task_dropped(self):
        output = {"tasks": [
            None,
            {"date": (self.today + timedelta(days=10)).isoformat()},
        ]}
        assert evals.dates_in_range(self.input, output) == 0.5

    def test_no_tasks_scores_zero(self):
        assert evals.dates_in_range(self.input, {"tasks": []}) == 0.0

    def test_missing_election_date_returns_zero_not_raise(self):
        # Hardened in this branch: a malformed dataset row no longer crashes
        # the entire eval.
        output = {"tasks": [{"date": (self.today + timedelta(days=10)).isoformat()}]}
        assert evals.dates_in_range({}, output) == 0.0
        assert evals.dates_in_range({"electionDate": "garbage"}, output) == 0.0


class TestUrlsValid:
    def test_https_is_valid(self):
        output = {"tasks": [{"url": "https://example.com"}]}
        assert evals.urls_valid(None, output) == 1.0

    def test_http_is_valid(self):
        output = {"tasks": [{"url": "http://example.com"}]}
        assert evals.urls_valid(None, output) == 1.0

    def test_null_url_is_valid(self):
        # Per the prompt rules, missing URL is acceptable.
        output = {"tasks": [{"url": None}]}
        assert evals.urls_valid(None, output) == 1.0

    def test_ftp_url_invalid(self):
        output = {"tasks": [{"url": "ftp://example.com"}]}
        assert evals.urls_valid(None, output) == 0.0

    def test_mixed(self):
        output = {"tasks": [
            {"url": "https://valid.com"},
            {"url": "javascript:alert(1)"},
            {"url": None},
            {"url": "https://another.com"},
        ]}
        assert evals.urls_valid(None, output) == 0.75

    def test_non_dict_task_dropped(self):
        output = {"tasks": [None, {"url": "https://valid.com"}]}
        # 1 valid out of 2 total — non-dict still counts toward the
        # denominator (effectively invalid) since the scorer divides
        # ok by len(tasks), not by the count of dict-shaped tasks.
        assert evals.urls_valid(None, output) == 0.5

    def test_no_tasks_scores_zero(self):
        assert evals.urls_valid(None, {"tasks": []}) == 0.0


class TestTitleOverlap:
    def test_returns_zero_when_no_expected(self):
        output = {"tasks": [{"title": "X"}]}
        assert evals.title_overlap(None, output, None) == 0.0
        assert evals.title_overlap(None, output, {}) == 0.0
        assert evals.title_overlap(None, output, {"tasks": []}) == 0.0

    def test_full_overlap(self):
        output = {"tasks": [{"title": "Pride"}, {"title": "BBQ"}]}
        expected = {"tasks": [{"title": "Pride"}, {"title": "BBQ"}]}
        assert evals.title_overlap(None, output, expected) == 1.0

    def test_case_insensitive(self):
        output = {"tasks": [{"title": "PRIDE"}]}
        expected = {"tasks": [{"title": "pride"}]}
        assert evals.title_overlap(None, output, expected) == 1.0

    def test_partial_overlap_jaccard(self):
        # Output: {a, b}, Expected: {b, c}. Intersection=1, Union=3 → 1/3.
        output = {"tasks": [{"title": "A"}, {"title": "B"}]}
        expected = {"tasks": [{"title": "B"}, {"title": "C"}]}
        assert abs(evals.title_overlap(None, output, expected) - 1 / 3) < 1e-9

    def test_no_overlap(self):
        output = {"tasks": [{"title": "A"}]}
        expected = {"tasks": [{"title": "B"}]}
        assert evals.title_overlap(None, output, expected) == 0.0

    def test_non_dict_tasks_skipped(self):
        output = {"tasks": [None, {"title": "X"}]}
        expected = {"tasks": [{"title": "X"}]}
        assert evals.title_overlap(None, output, expected) == 1.0


class FakePromptParam:
    """Stand-in for a Braintrust playground `type: 'prompt'` parameter.
    Records every `.build(**kwargs)` call so tests can assert which
    variables flowed in. Returns a messages-shaped dict that
    `flatten_prompt_messages` will collapse into the content string."""

    def __init__(self, content: str):
        self.content = content
        self.build_calls: list[dict] = []

    def build(self, **kwargs):
        self.build_calls.append(kwargs)
        return {"messages": [{"role": "system", "content": self.content}]}


class FakeHooks:
    def __init__(self):
        self.parameters = {
            "search_prompt": FakePromptParam("irrelevant"),
            "filter_prompt": FakePromptParam("irrelevant"),
        }


class TestPipelineTask:
    """`pipeline_task` short-circuits with an empty result when the
    dataset row is missing or has a malformed `electionDate`. This is
    deliberate handling for a known prod data gap (~80% of pro
    campaigns lack `details.electionDate`) — without it, one bad row
    crashes an entire eval run. Locked here so the branches don't get
    refactored away as dead code."""

    @pytest.mark.asyncio
    async def test_short_circuits_on_missing_election_date(self):
        hooks = FakeHooks()
        result = await evals.pipeline_task({"city": "Boston"}, hooks)

        assert result == {"tasks": []}
        # Bailed before prompt rendering or any LLM work.
        assert hooks.parameters["search_prompt"].build_calls == []

    @pytest.mark.asyncio
    async def test_short_circuits_on_malformed_election_date(self):
        hooks = FakeHooks()
        result = await evals.pipeline_task({"electionDate": "not-a-date"}, hooks)

        assert result == {"tasks": []}
        assert hooks.parameters["search_prompt"].build_calls == []


def _walk(value, path=""):
    """Yield (jsonpath, value) for every leaf and container node in a
    nested structure. Used by the no-null assertion below to point at the
    exact field that broke if the test ever fails."""
    if isinstance(value, dict):
        yield path or "$", value
        for k, v in value.items():
            yield from _walk(v, f"{path}.{k}" if path else f"$.{k}")
    elif isinstance(value, list):
        yield path or "$", value
        for i, v in enumerate(value):
            yield from _walk(v, f"{path}[{i}]")
    else:
        yield path or "$", value


class TestEvalParametersShape:
    """Regression coverage for the parameter-serialization bug that hid
    the sandbox from the playground "+ Task → Remote eval" picker.

    Background: a previous revision built `EVAL_PARAMETERS` with the
    Braintrust SDK's typed constructors (PromptParameter / PromptData /
    PromptChatBlock / PromptMessage) for static-type-check coverage.
    Those dataclasses include Optional fields that default to `None`
    (`tools`, `name`, `function_call`, `tool_calls`), and the SDK's
    serializer emits them as JSON `null`. The Braintrust playground's
    parameter validator silently rejects shapes containing those nulls —
    the function still registers via the API, but it doesn't surface in
    the playground task picker. We now use plain dict literals, which
    omit unset keys entirely.

    These tests lock both ends of the contract:
      1. `test_no_null_anywhere` proves our dict has no null leaves at
         any depth — catches anyone reverting to typed constructors or
         introducing a `None` default in the dict.
      2. `test_typed_constructors_still_emit_nulls` is documentary: it
         shows what the SDK serializer produces for an unset
         PromptMessage. If Braintrust ever fixes the serializer to omit
         unset fields, this test fails — at which point we can
         reconsider using the typed constructors for static type
         coverage."""

    def test_no_null_anywhere(self):
        for path, value in _walk(evals.EVAL_PARAMETERS):
            assert value is not None, (
                f"EVAL_PARAMETERS contains None at {path}; the Braintrust "
                f"playground will hide the sandbox from the '+ Task → "
                f"Remote eval' picker. Omit the key entirely instead."
            )

    def test_top_level_has_both_prompt_params(self):
        # Cheap sanity: if the dict gets refactored, make sure both
        # prompts the PM iterates on are still wired through.
        assert set(evals.EVAL_PARAMETERS) == {"search_prompt", "filter_prompt"}

    def test_typed_constructors_still_emit_nulls(self):
        """Documentary — locks in the SDK behavior we're working around.

        If this test starts failing, the Braintrust SDK has changed how
        it serializes Optional/None fields. At that point the typed
        constructors might be safe to use again, and we should re-evaluate
        whether to bring back the static-type coverage they provided."""
        from dataclasses import asdict

        from braintrust.prompt import PromptChatBlock, PromptMessage

        block = PromptChatBlock(
            messages=[PromptMessage(role="system", content="hello")],
        )
        serialized = asdict(block)

        # The PromptMessage default-None fields all surface as keys with
        # null values in the serialization — this is the shape that broke
        # the playground.
        msg = serialized["messages"][0]
        assert "name" in msg and msg["name"] is None
        assert "function_call" in msg and msg["function_call"] is None
        assert "tool_calls" in msg and msg["tool_calls"] is None
        # PromptChatBlock's own `tools` field too.
        assert "tools" in serialized and serialized["tools"] is None
