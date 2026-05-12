"""
Braintrust Sandbox Eval for the campaign plan pipeline.

Runs end-to-end (search → filter → cleanup). The PM edits the two prompt
templates directly in the playground form (UI prompt editor), runs against
a curated dataset, sees scored results. No prompt-version management
required — eval changes never touch the prod prompts in the Braintrust
prompt registry.

Push (engineer, after code changes):
    ./campaign_plan_lambda/push_eval_to_braintrust.sh

PM workflow (in Braintrust UI):
    Project → Playground → + Task → Remote eval submenu → pick this eval →
    edit search_prompt and/or filter_prompt → pick dataset → Run.

Promotion to prod is a deliberate engineer step:
    Once a prompt is proven via eval, save the content into the Braintrust
    prompt registry under the slugs `search-community-events` and
    `filter-and-structure-events`. The Lambda picks up the latest version
    via load_prompt_from_braintrust on the next invocation.

    Caveat: prompt updates take effect immediately on the next Lambda call.
    Environment-pinned prompt deployment is a future ticket — until then,
    treat the prompt registry as a "production-affecting" surface.

Environment configuration:
    Set `ENVIRONMENT=eval` in Braintrust → Project Settings → Environment
    Variables (alongside GEMINI_API_KEY). This stamps `environment=eval`
    on every span emitted from sandbox runs so they're filterable in Logs
    separately from prod runs (which set ENVIRONMENT via the Lambda's
    AWS Lambda env config). For local `bt eval --dev` invocations, export
    ENVIRONMENT=eval in your shell or .env.
"""

import os
import re
from datetime import date
from typing import Any, Optional

from braintrust import Eval, init_dataset

from shared.braintrust import (
    flatten_prompt_messages,
    init_braintrust,
    trace_pipeline,
)
from shared.llm_gemini_3 import Gemini3Client
from campaign_plan_lambda.event_generator import (
    FILTER_PROMPT_FALLBACK,
    SEARCH_PROMPT_FALLBACK,
    _build_prompt_variables,
    _filter_and_structure_events,
    _search_community_events,
)

PROJECT = "campaign-plan"
DATASET_NAME = "campaign-plan-pipeline"


def _to_mustache(template: str) -> str:
    """Convert Python `{var}` placeholders to Mustache `{{var}}` so the
    Lambda's fallback prompts (rendered via `.format(**vars)`) can serve
    as the eval-form defaults (rendered via Braintrust's Mustache engine)
    without manual duplication. Already-doubled braces are preserved."""
    return re.sub(
        r"(?<!\{)\{([a-zA-Z_][a-zA-Z0-9_]*)\}(?!\})",
        r"{{\1}}",
        template,
    )


# Default prompt content for the playground form, derived from the
# Lambda's runtime fallback prompts. Edit `SEARCH_PROMPT_FALLBACK` /
# `FILTER_PROMPT_FALLBACK` in event_generator.py and these defaults
# track automatically — no manual sync. The PM can still override per-
# run via the form.
SEARCH_PROMPT_DEFAULT = _to_mustache(SEARCH_PROMPT_FALLBACK)
FILTER_PROMPT_DEFAULT = _to_mustache(FILTER_PROMPT_FALLBACK)


def _coerce_optional_str(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, str):
        return v if v.strip() else None
    return str(v)


async def pipeline_task(input: dict, hooks: Any) -> dict:
    """Run the full search → filter pipeline against one dataset row, using
    prompts supplied by the PM via the playground form.

    Wraps the work in a `generate_event_tasks` span (same name and input
    shape as the Lambda's parent span) so the project Logs view shows the
    variable-dict input + cleaned-tasks output at the top, with the inner
    LLM calls nested underneath. Eval-vs-prod distinguishability comes
    from `metadata.environment="eval"` (set in Braintrust project env
    vars) — no separate tag is needed."""
    # Initialise the BraintrustClient singleton so _traced_call inside
    # Gemini3Client emits child spans under the experiment row.
    # Idempotent — the singleton no-ops on repeat calls — and intentionally
    # placed here rather than at module import to keep eval imports side-
    # effect-free (the bundler imports this file with no API key set).
    init_braintrust(project=PROJECT)

    params = hooks.parameters

    # Some prod campaigns lack details.electionDate. Curated dataset rows
    # may inherit that gap. Skip the LLM work and return an empty result so
    # one bad row doesn't crash the whole eval. Scorers treat empty `tasks`
    # as a 0 score, which is the right signal here.
    raw_election_date = input.get("electionDate")
    if not raw_election_date:
        return {"tasks": []}
    try:
        election_date = date.fromisoformat(raw_election_date)
    except (ValueError, TypeError):
        return {"tasks": []}
    today = date.today()
    variables = _build_prompt_variables(
        today=today,
        election_date=election_date,
        state=_coerce_optional_str(input.get("state")),
        city=_coerce_optional_str(input.get("city")),
        office_name=_coerce_optional_str(input.get("officeName")),
        office_level=_coerce_optional_str(input.get("officeLevel")),
        primary_election_date=_coerce_optional_str(input.get("primaryElectionDate")),
    )

    # Match the Lambda's parent-span input dict so eval and prod traces
    # have identical shapes in the Logs view.
    pipeline_input = {
        "electionDate": str(election_date),
        "state": _coerce_optional_str(input.get("state")),
        "city": _coerce_optional_str(input.get("city")),
        "officeName": _coerce_optional_str(input.get("officeName")),
        "officeLevel": _coerce_optional_str(input.get("officeLevel")),
        "primaryElectionDate": _coerce_optional_str(input.get("primaryElectionDate")),
    }

    llm_client = Gemini3Client()

    with trace_pipeline(
        "generate_event_tasks",
        metadata={
            "model": llm_client.default_model.value,
            "environment": os.getenv("ENVIRONMENT", "eval"),
        },
    ) as span:
        span.log(input=pipeline_input)

        search_built = params["search_prompt"].build(**variables)
        search_text = flatten_prompt_messages(search_built)
        # hooks.span.start_span attaches to the experiment row so the eval
        # drill-down shows the per-step breakdown. The LLM call inside
        # routes via `_traced_call` into the project Logs view (under
        # generate_event_tasks), so both views stay populated.
        with hooks.span.start_span(name="search") as search_span:
            search_span.log(input={"prompt": search_text})
            raw_events = await _search_community_events(
                llm_client, variables, rendered_prompt=search_text,
            )
            search_span.log(output={"raw_events": raw_events})

        filter_built = params["filter_prompt"].build(**variables, raw_events=raw_events)
        filter_text = flatten_prompt_messages(filter_built)
        with hooks.span.start_span(name="filter") as filter_span:
            filter_span.log(input={"prompt": filter_text})
            tasks = await _filter_and_structure_events(
                llm_client, variables, election_date, today, raw_events,
                rendered_prompt=filter_text,
            )
            filter_span.log(output={"tasks": [t.model_dump() for t in tasks]})

        output = {"tasks": [t.model_dump() for t in tasks]}
        span.log(output=output)

    return output


# --- Scorers --------------------------------------------------------------
# Each scorer returns a float in [0, 1]. Braintrust uses the function name
# as the metric name in experiment summaries. Signature follows Braintrust's
# canonical scorer contract: (input, output, expected, **kwargs).


def count_in_range(input: dict, output: dict, expected: Optional[dict] = None, **_: Any) -> float:
    """Prompt asks for 5–8 events. Score 1 if in range, otherwise scaled by
    distance from the band."""
    n = len(output.get("tasks") or [])
    if 5 <= n <= 8:
        return 1.0
    if n == 0:
        return 0.0
    if n < 5:
        return n / 5
    return max(0.0, 1.0 - (n - 8) / 8)


def dates_in_range(input: dict, output: dict, expected: Optional[dict] = None, **_: Any) -> float:
    """Every task date must fall between today and the election date. The
    upper bound comes from the dataset row's electionDate; the lower bound
    is today (the day the eval runs) — events in the past don't help the
    candidate even if the model produced them."""
    try:
        election = date.fromisoformat(input["electionDate"])
    except (KeyError, ValueError, TypeError):
        # Dataset row missing / malformed electionDate — skip with a 0
        # score rather than crash the whole row.
        return 0.0
    today = date.today()
    tasks = output.get("tasks") or []
    if not tasks:
        return 0.0
    ok = 0
    for t in tasks:
        if not isinstance(t, dict):
            continue
        try:
            d = date.fromisoformat(t["date"])
        except (ValueError, KeyError, TypeError):
            continue
        if today <= d <= election:
            ok += 1
    return ok / len(tasks)


def urls_valid(input: dict, output: dict, expected: Optional[dict] = None, **_: Any) -> float:
    """Non-null URLs must be http/https. Null URLs are fine."""
    tasks = output.get("tasks") or []
    if not tasks:
        return 0.0
    ok = 0
    for t in tasks:
        if not isinstance(t, dict):
            continue
        url = t.get("url")
        if url is None or (isinstance(url, str) and url.startswith(("http://", "https://"))):
            ok += 1
    return ok / len(tasks)


def title_overlap(input: dict, output: dict, expected: Optional[dict] = None, **_: Any) -> float:
    """Jaccard of task titles (case-insensitive) vs expected. Skip if no
    expected (PM may curate inputs without writing gold outputs)."""
    if not expected or not expected.get("tasks"):
        return 0.0
    out_titles = {
        (t.get("title") or "").strip().lower()
        for t in (output.get("tasks") or [])
        if isinstance(t, dict)
    }
    exp_titles = {
        (t.get("title") or "").strip().lower()
        for t in expected["tasks"]
        if isinstance(t, dict)
    }
    out_titles.discard("")
    exp_titles.discard("")
    if not out_titles or not exp_titles:
        return 0.0
    return len(out_titles & exp_titles) / len(out_titles | exp_titles)


# --- Eval -----------------------------------------------------------------
# `data=` is ignored when the eval runs from the playground (PM picks the
# dataset there), but it must still be valid Python so offline runs work.
#
# Use plain dict literals for `EVAL_PARAMETERS` rather than Braintrust's
# typed constructors (PromptParameter / PromptData / PromptChatBlock /
# PromptMessage). Those constructors emit every field — including unset
# Optional ones — as JSON `null`s (e.g. `tools: null`, `name: null`,
# `tool_calls: null`), which the playground UI's parameter validator
# rejects: a sandbox registered with the typed-constructor shape silently
# disappears from the playground "+ Task → Remote eval" picker even
# though the function is queryable via the API. Dict literals omit the
# unset keys, matching what the UI expects.
#
# Pulled out as a module-level constant so `tests/test_evals.py` can
# import it and assert the no-null shape directly (regression check).
EVAL_PARAMETERS: dict[str, Any] = {
    "search_prompt": {
        "type": "prompt",
        "name": "Search prompt",
        "description": (
            "Prompt for the Gemini grounded-search step. Variables "
            "available: today, election_date, state, city, office_name, "
            "office_level, primary_election_date."
        ),
        "default": {
            "prompt": {
                "type": "chat",
                "messages": [{"role": "system", "content": SEARCH_PROMPT_DEFAULT}],
            },
            "options": {"model": "gemini-3-flash-preview"},
        },
    },
    "filter_prompt": {
        "type": "prompt",
        "name": "Filter prompt",
        "description": (
            "Prompt for the structured-output filter step. Variables "
            "available: today, election_date, state, city, office_name, "
            "office_level, primary_election_date, raw_events (the search "
            "step's output)."
        ),
        "default": {
            "prompt": {
                "type": "chat",
                "messages": [{"role": "system", "content": FILTER_PROMPT_DEFAULT}],
            },
            "options": {"model": "gemini-3-flash-preview"},
        },
    },
}


Eval(
    PROJECT,
    experiment_name="campaign-plan-pipeline",
    data=init_dataset(PROJECT, DATASET_NAME),
    task=pipeline_task,
    scores=[count_in_range, dates_in_range, urls_valid, title_overlap],
    parameters=EVAL_PARAMETERS,
)
