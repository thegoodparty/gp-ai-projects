"""
Generic Braintrust Sandbox Eval for prompt iteration.

Wraps Gemini grounded search + Gemini structured output as a parameterized
two-stage pipeline so PMs can A/B test arbitrary prompts in the Braintrust
playground without engineering work per new prompt. The campaign_plan_lambda
eval inspired this — same shape, but prompts and schemas are inputs rather
than baked in.

Push (engineer, after code changes):
    ./braintrust_eval_sandbox/push_eval_to_braintrust.sh

PM workflow (in Braintrust UI):
    Project "braintrust-eval-sandbox" → Playground → + Task → Remote eval
    submenu → pick this eval → edit the four parameters → pick a dataset →
    Run.

The four parameters:
    main_prompt_search_enabled  bool      Toggle Gemini grounded search on
                                          stage 1. Off = plain completion.
    main_prompt                 prompt    Stage 1 prompt template. Mustache
                                          vars resolve against the dataset
                                          row's `input` dict.
    structured_output_prompt    prompt    Stage 2 prompt template. Same
                                          Mustache resolution, plus the
                                          reserved `{{main_prompt_output}}`
                                          variable holding stage 1's text.
    structured_output_schema    json      JSON Schema for stage 2 output.
                                          Leave empty ({}) to skip stage 2
                                          and return stage 1's text directly.

Output shape:
    {"output": <str>}   when stage 2 is skipped
    {"output": <dict>}  when stage 2 ran, matching the provided schema

Mustache variable contract:
    Bare `{{var}}` references in either prompt require a matching key in the
    dataset row's `input`. Missing key → ValueError that names the prompt and
    the missing var. To opt out of strictness, use Mustache section syntax:
    `{{#var}}value: {{var}}{{/var}}` renders nothing when `var` is missing or
    falsy. Extras in the row (keys the prompts don't reference) are ignored.

Environment configuration:
    Set `GEMINI_API_KEY` and `ENVIRONMENT=eval` in Braintrust → Project
    Settings → Environment Variables. The ENVIRONMENT tag stamps every span
    with `metadata.environment="eval"` for filtering in Logs.
"""

import os
import re
from typing import Any

from braintrust import Eval, init_dataset

from shared.braintrust import (
    flatten_prompt_messages,
    init_braintrust,
    trace_pipeline,
)
from shared.llm_gemini_3 import Gemini3Client

PROJECT = "braintrust-eval-sandbox"
DATASET_NAME = "braintrust-eval-sandbox"

# Reserved variable name stage 2 prompts use to reference stage 1's raw output.
# Excluded from the missing-key check on the structured-output prompt because
# the task supplies it, not the dataset row.
MAIN_PROMPT_OUTPUT_VAR = "main_prompt_output"

# Matches bare `{{var}}` references but not `{{#var}}`, `{{/var}}`, `{{^var}}`,
# `{{!comment}}`, `{{>partial}}`, or `{{&unescaped}}` — those start with a
# non-alphanumeric sigil, which the leading character class excludes. Section
# vars (#, /, ^) are inherently optional in Mustache; we honor that and only
# enforce presence for bare references.
_BARE_VAR_RE = re.compile(r"\{\{\s*([a-zA-Z_]\w*)\s*\}\}")


def _bare_vars(template: str) -> set[str]:
    return set(_BARE_VAR_RE.findall(template))


def _require_keys(template: str, row_keys: set[str], *, label: str, reserved: set[str]) -> None:
    referenced = _bare_vars(template) - reserved
    missing = referenced - row_keys
    if missing:
        raise ValueError(
            f"{label} references {sorted(missing)} but the dataset row has no such key(s). "
            f"Row keys: {sorted(row_keys)}. "
            f"To make a variable optional, use Mustache section syntax: "
            f"`{{{{#var}}}}...{{{{/var}}}}` renders nothing when the key is missing or falsy."
        )


async def pipeline_task(input: dict, hooks: Any) -> dict:
    """Run the generic two-stage pipeline against one dataset row.

    Stage 1 calls Gemini with or without grounded search per the toggle.
    Stage 2 calls Gemini structured-output mode when a schema is provided;
    otherwise stage 1's text is returned directly.

    init_braintrust is invoked here (not at import) so the bundler can
    import this file without an API key set — the push CLI imports with
    no env, and shared/braintrust.py's singleton no-ops without one.
    """
    init_braintrust(project=PROJECT)

    params = hooks.parameters
    search_enabled: bool = bool(params.get("main_prompt_search_enabled", False))
    schema: dict = params.get("structured_output_schema") or {}

    row_keys = set(input.keys())

    main_prompt_built = params["main_prompt"].build(**input)
    main_prompt_text = flatten_prompt_messages(main_prompt_built)
    _require_keys(main_prompt_text, row_keys, label="main_prompt", reserved=set())

    llm_client = Gemini3Client()

    with trace_pipeline(
        "pipeline_task",
        metadata={
            "model": llm_client.default_model.value,
            "environment": os.getenv("ENVIRONMENT", "eval"),
            "search_enabled": search_enabled,
            "has_schema": bool(schema),
        },
    ) as span:
        span.log(input=dict(input))

        with hooks.span.start_span(name="main_prompt") as main_span:
            main_span.log(input={"prompt": main_prompt_text, "search_enabled": search_enabled})
            if search_enabled:
                main_response = llm_client.generate_with_search(main_prompt_text)
                main_text = main_response.text
            else:
                main_text = llm_client.generate_content(main_prompt_text)
            main_span.log(output={"text": main_text})

        if not schema:
            output: dict = {"output": main_text}
            span.log(output=output)
            return output

        struct_input = {**input, MAIN_PROMPT_OUTPUT_VAR: main_text}
        struct_built = params["structured_output_prompt"].build(**struct_input)
        struct_text = flatten_prompt_messages(struct_built)
        _require_keys(
            struct_text,
            row_keys,
            label="structured_output_prompt",
            reserved={MAIN_PROMPT_OUTPUT_VAR},
        )

        with hooks.span.start_span(name="structured_output") as struct_span:
            struct_span.log(input={"prompt": struct_text, "schema": schema})
            structured = llm_client.generate_structured_content(
                prompt=struct_text,
                response_schema=schema,
                temperature=0.0,
            )
            struct_span.log(output={"structured": structured})

        output = {"output": structured}
        span.log(output=output)
        return output


# Plain dict literals — not typed constructors — so unset Optional fields
# don't serialize as JSON `null`s and trip the playground's parameter
# validator (which silently rejects the function from the "+ Task → Remote
# eval" picker). Same shape rule as campaign_plan_lambda/evals.py.
EVAL_PARAMETERS: dict[str, Any] = {
    "main_prompt_search_enabled": {
        "type": "boolean",
        "name": "Main prompt: enable Google Search grounding",
        "description": (
            "Toggle Gemini grounded search on the stage 1 (main) prompt. "
            "Off = plain completion."
        ),
        "default": True,
    },
    "main_prompt": {
        "type": "prompt",
        "name": "Main prompt",
        "description": (
            "Stage 1 prompt. Mustache variables resolve against the dataset "
            "row's `input` dict. Bare {{var}} references are required; "
            "{{#var}}...{{/var}}` sections are optional."
        ),
        "default": {
            "prompt": {
                "type": "chat",
                "messages": [{"role": "system", "content": ""}],
            },
            "options": {"model": "gemini-3-flash-preview"},
        },
    },
    "structured_output_prompt": {
        "type": "prompt",
        "name": "Structured output prompt",
        "description": (
            "Stage 2 prompt that reformats stage 1's output into structured "
            "JSON. Reference stage 1's text as {{main_prompt_output}}. "
            "Skipped when structured_output_schema is empty."
        ),
        "default": {
            "prompt": {
                "type": "chat",
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Extract structured data from the following text.\n\n"
                            "{{main_prompt_output}}"
                        ),
                    }
                ],
            },
            "options": {"model": "gemini-3-flash-preview"},
        },
    },
    "structured_output_schema": {
        "type": "json",
        "name": "Structured output schema",
        "description": (
            "JSON Schema for stage 2 output. Leave empty ({}) to skip stage "
            "2 and return stage 1's text directly."
        ),
        "default": {},
    },
}


# `data=` is ignored when the eval runs from the playground (PM picks a
# dataset there). It still has to be a valid call for offline runs to work.
# init_dataset will lazily create the dataset on first read if absent.
Eval(
    PROJECT,
    experiment_name="braintrust-eval-sandbox",
    data=init_dataset(PROJECT, DATASET_NAME),
    task=pipeline_task,
    scores=[],
    parameters=EVAL_PARAMETERS,
)
