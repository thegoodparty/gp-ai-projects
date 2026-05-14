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
from pydantic import BaseModel, Field

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
# non-alphanumeric sigil, which the leading character class excludes.
_BARE_VAR_RE = re.compile(r"\{\{\s*([a-zA-Z_]\w*)\s*\}\}")

# Matches section openers `{{#var}}` and inverted-section openers `{{^var}}`.
# Both forms guard their contents, so any variable named here is treated as
# optional — even when bare {{var}} references appear inside the section
# body (a common pattern: `{{#name}}Hello {{name}}!{{/name}}`).
_SECTION_VAR_RE = re.compile(r"\{\{\s*[#^]\s*([a-zA-Z_]\w*)\s*\}\}")


def _bare_vars(template: str) -> set[str]:
    return set(_BARE_VAR_RE.findall(template))


def _section_vars(template: str) -> set[str]:
    return set(_SECTION_VAR_RE.findall(template))


def _extract_template_text(prompt_obj: Any) -> str:
    """Concatenate the raw Mustache template strings from a Braintrust Prompt
    object, before any rendering. The strict-key check runs on this — parsing
    the post-render output would miss already-resolved-or-empty variables
    (which is exactly the silent-failure mode we want to catch).

    Handles both chat prompts (joins each message's content) and completion
    prompts (single content string). Multimodal content parts (text/image
    lists) are flattened to their text components.
    """
    inner = getattr(prompt_obj, "prompt", None)
    if inner is None:
        return ""
    inner_type = getattr(inner, "type", None)
    if inner_type == "completion":
        return getattr(inner, "content", "") or ""
    if inner_type == "chat":
        parts: list[str] = []
        for msg in getattr(inner, "messages", None) or []:
            content = getattr(msg, "content", None)
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                # Multimodal message: pick up the text parts. Content pieces
                # come in three shapes: bare strings, dicts (`{"type": "text",
                # "text": "..."}` — the JSON-on-the-wire form), and TextPart
                # dataclass objects. Skipping the dict shape would silently
                # bypass the strict-key check for any prompt authored as
                # JSON, which is exactly the failure mode this function is
                # supposed to prevent.
                for piece in content:
                    if isinstance(piece, str):
                        parts.append(piece)
                    elif isinstance(piece, dict):
                        text = piece.get("text")
                        if isinstance(text, str):
                            parts.append(text)
                    else:
                        text = getattr(piece, "text", None)
                        if isinstance(text, str):
                            parts.append(text)
        return "\n".join(parts)
    return ""


_UNSUPPORTED_SCHEMA_KEYWORDS = ("$ref", "$defs", "definitions")


def _normalize_schema_for_gemini(node: Any) -> Any:
    """Rewrite standard JSON Schema into Gemini's structured-output dialect.

    Gemini's types.Schema only accepts singular `type` values (STRING, NUMBER,
    INTEGER, BOOLEAN, ARRAY, OBJECT, NULL) plus a separate `nullable` flag.
    Standard JSON Schema expresses optionality as a type union — e.g.
    `{"type": ["string", "null"]}` — which is what OpenAI's structured-output
    API emits and what most PMs paste. This walker rewrites that union to
    `{"type": "string", "nullable": true}`. Already-Gemini-form schemas
    (with `nullable` already set) pass through unchanged.

    Other shapes that raise here instead of letting the Gemini SDK fail with
    an opaque pydantic traceback:
      - Genuine type unions like `["string", "number"]` — Gemini can't
        express them.
      - Bare `["null"]` — pick a concrete type and use `nullable: true`.
      - `$ref` / `$defs` / `definitions` — refs aren't resolved here; inline
        the referenced schema (commonly produced by Pydantic's
        `model_json_schema()`, which a PM may paste verbatim).

    Walks dicts and lists recursively; other node types pass through as-is.
    """
    if isinstance(node, list):
        return [_normalize_schema_for_gemini(item) for item in node]
    if not isinstance(node, dict):
        return node

    # Fail fast on unsupported keywords before doing any work. Checked at
    # every recursion level so a $ref buried inside `properties` still fires.
    for unsupported in _UNSUPPORTED_SCHEMA_KEYWORDS:
        if unsupported in node:
            raise ValueError(
                f"Gemini structured output does not support `{unsupported}`. "
                f"Inline the referenced schema directly. If this came from "
                f"Pydantic's `model_json_schema()`, expand `$defs` into the "
                f"schema body before pasting."
            )

    result = {k: _normalize_schema_for_gemini(v) for k, v in node.items()}

    type_val = result.get("type")
    if isinstance(type_val, list):
        # Empty list provides no type information. Falling through here would
        # leave `type: []` in the result and surface as an opaque pydantic
        # validation error in the Gemini SDK; raise with a clear message
        # instead. Checked before the null-extraction branches so the error
        # references the original input.
        if len(type_val) == 0:
            raise ValueError(
                "Gemini structured output requires a concrete `type`: got an "
                "empty list. Specify one type (optionally with `nullable: true`)."
            )
        non_null = [t for t in type_val if t != "null"]
        has_null = len(non_null) != len(type_val)
        if has_null and len(non_null) == 1:
            # Standard nullable form: ["X", "null"] -> "X" with nullable flag.
            result["type"] = non_null[0]
            result["nullable"] = True
        elif not has_null and len(non_null) == 1:
            # Trivial single-element list: ["X"] -> "X". Some generators emit
            # this; Gemini's SDK rejects lists outright.
            result["type"] = non_null[0]
        elif has_null and len(non_null) == 0:
            raise ValueError(
                f"Gemini structured output does not support a bare null type: "
                f"{type_val!r}. Use a concrete type combined with "
                f"`nullable: true` instead."
            )
        elif len(non_null) > 1:
            raise ValueError(
                f"Gemini structured output does not support type unions: {type_val!r}. "
                f"Use a single `type` value (optionally combined with `nullable: true` "
                f"for nullable fields)."
            )

    return result


def _require_keys(template: str, row_keys: set[str], *, label: str, reserved: set[str]) -> None:
    # A variable declared as a section anywhere in the template is optional
    # everywhere — even at bare-reference sites. Matches the user mental
    # model "I wrapped it in a section, so it doesn't need to be in the row."
    optional = _section_vars(template)
    referenced = _bare_vars(template) - reserved - optional
    missing = referenced - row_keys
    if missing:
        raise ValueError(
            f"{label} references {sorted(missing)} but the dataset row has no such key(s). "
            f"Row keys: {sorted(row_keys)}. "
            f"To make a variable optional, use Mustache section syntax: "
            f"`{{{{#var}}}}...{{{{/var}}}}` renders nothing when the key is missing or falsy."
        )


def pipeline_task(input: dict, hooks: Any) -> dict:
    """Run the generic two-stage pipeline against one dataset row.

    Stage 1 calls Gemini with or without grounded search per the toggle.
    Stage 2 calls Gemini structured-output mode when a schema is provided;
    otherwise stage 1's text is returned directly.

    init_braintrust is invoked here (not at import) so the bundler can
    import this file without an API key set — the push CLI imports with
    no env, and shared/braintrust.py's singleton no-ops without one.
    """
    # Reject a row that collides with the reserved stage-1-output variable
    # before any LLM work. Without this, stage 2 would silently overwrite
    # the row's `main_prompt_output` key, and the PM would never know their
    # dataset column had been discarded.
    if MAIN_PROMPT_OUTPUT_VAR in input:
        raise ValueError(
            f"Dataset row has a key named '{MAIN_PROMPT_OUTPUT_VAR}', which "
            f"is reserved for stage 1's output (it gets injected into the "
            f"structured-output prompt). Rename the row column."
        )

    init_braintrust(project=PROJECT)

    params = hooks.parameters
    search_enabled: bool = bool(params.get("main_prompt_search_enabled", False))
    schema: dict = params.get("structured_output_schema") or {}

    row_keys = set(input.keys())

    # Pre-render check: parse {{var}} references out of the raw template
    # and verify the row supplies each one. Catches "I forgot to populate
    # `city` in the dataset" before we burn an LLM call rendering empty
    # placeholders.
    main_template = _extract_template_text(params["main_prompt"])
    _require_keys(main_template, row_keys, label="main_prompt", reserved=set())

    main_prompt_built = params["main_prompt"].build(**input)
    main_prompt_text = flatten_prompt_messages(main_prompt_built)

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

        struct_template = _extract_template_text(params["structured_output_prompt"])
        _require_keys(
            struct_template,
            row_keys,
            label="structured_output_prompt",
            reserved={MAIN_PROMPT_OUTPUT_VAR},
        )

        struct_input = {**input, MAIN_PROMPT_OUTPUT_VAR: main_text}
        struct_built = params["structured_output_prompt"].build(**struct_input)
        struct_text = flatten_prompt_messages(struct_built)

        # Translate JSON Schema → Gemini schema. PMs paste the OpenAI/Anthropic
        # flavor; Gemini's SDK needs nullable as a flag, not a union member.
        gemini_schema = _normalize_schema_for_gemini(schema)

        with hooks.span.start_span(name="structured_output") as struct_span:
            struct_span.log(input={"prompt": struct_text, "schema": gemini_schema})
            structured = llm_client.generate_structured_content(
                prompt=struct_text,
                response_schema=gemini_schema,
                temperature=0.0,
            )
            struct_span.log(output={"structured": structured})

        output = {"output": structured}
        span.log(output=output)
        return output


# Braintrust's serializer dispatches on parameter "type":
#   - "prompt" / "model" → dict literal accepted as-is
#   - everything else → must be a Pydantic model class. The single-field-
#     named-"value" convention unwraps so the UI shows a flat scalar/object
#     editor (not a nested {value: ...} form). The unwrapped value is what
#     arrives at the task via hooks.parameters.
#
# Why not typed constructors for the prompt entries? The campaign_plan_lambda
# eval documents this: Braintrust's PromptParameter / PromptData /
# PromptChatBlock / PromptMessage dataclasses serialize unset Optional fields
# as JSON `null`s, which the playground's parameter validator silently
# rejects (function disappears from the "+ Task → Remote eval" picker).
# Dict literals omit the unset keys.
class _SearchEnabledParam(BaseModel):
    value: bool = Field(
        default=True,
        title="Main prompt: enable Google Search grounding",
        description=(
            "Toggle Gemini grounded search on the stage 1 (main) prompt. "
            "Off = plain completion."
        ),
    )


class _StructuredOutputSchemaParam(BaseModel):
    value: dict[str, Any] = Field(
        default_factory=dict,
        title="Structured output schema",
        description=(
            "JSON Schema for stage 2 output. Leave empty ({}) to skip stage "
            "2 and return stage 1's text directly."
        ),
    )


EVAL_PARAMETERS: dict[str, Any] = {
    "main_prompt_search_enabled": _SearchEnabledParam,
    "main_prompt": {
        "type": "prompt",
        "name": "Main prompt",
        "description": (
            "Stage 1 prompt. Mustache variables resolve against the dataset "
            "row's `input` dict. Bare `{{var}}` references are required; "
            "`{{#var}}...{{/var}}` sections are optional."
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
    "structured_output_schema": _StructuredOutputSchemaParam,
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
