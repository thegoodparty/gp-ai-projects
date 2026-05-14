# Braintrust Eval Sandbox

A generic Braintrust sandbox eval that wraps Gemini grounded search + Gemini structured output as a two-stage pipeline driven by playground parameters. Lets PMs A/B test arbitrary prompts without engineering work per new prompt.

Inspired by `campaign_plan_lambda/evals.py`. Same architectural shape, but the prompts and schema are inputs rather than baked in.

## How it works

Each eval run takes four playground parameters:

| Parameter | Type | Purpose |
|-----------|------|---------|
| `main_prompt_search_enabled` | boolean | Toggle Gemini grounded search on stage 1. Off = plain completion. |
| `main_prompt` | prompt | Stage 1 prompt. Mustache variables resolve against the dataset row's `input` dict. |
| `structured_output_prompt` | prompt | Stage 2 prompt. Same resolution rules, plus a reserved `{{main_prompt_output}}` variable. |
| `structured_output_schema` | json | JSON Schema for stage 2 output. Leave empty (`{}`) to skip stage 2. |

Output shape:

```json
{"output": "stage 1 text"}                       // when stage 2 is skipped
{"output": {"name": "...", "date": "..."}}       // when stage 2 ran with a schema
```

## Schema authoring

Paste standard JSON Schema. The eval normalizes it to Gemini's structured-output dialect before calling the model — you do not need to know Gemini-specific syntax.

The main translation handled automatically: nullable fields. Standard JSON Schema expresses optionality as `{"type": ["string", "null"]}`. The eval rewrites that to Gemini's form `{"type": "string", "nullable": true}` on every nested field.

```json
// What you paste — standard JSON Schema
{
  "type": "object",
  "properties": {
    "name":    {"type": "string"},
    "score":   {"type": ["number", "null"]},
    "tags":    {"type": "array", "items": {"type": "string"}}
  },
  "required": ["name"]
}
```

Schemas already written in Gemini's flavor (`nullable: true`) pass through unchanged, so either form works.

What is **not** supported and will raise a clear error:
- Genuine type unions like `{"type": ["string", "number"]}` — Gemini cannot express them. Pick one type.
- `$ref` and `$defs` — refs aren't resolved; inline the referenced schema.

## PM workflow

1. Open the `braintrust-eval-sandbox` Braintrust project.
2. Playground → **+ Task** → Remote eval submenu → pick this eval.
3. Edit the four parameters. Pick a dataset (or curate one beforehand).
4. **Run**.

Promotion to deployed code is out of scope for this sandbox — once a prompt is proven, copy it into whichever consuming codepath (Lambda, service, etc.) you own.

## Engineer workflow

Push code changes:

```bash
./braintrust_eval_sandbox/push_eval_to_braintrust.sh
```

Requires `BRAINTRUST_API_KEY` in `.env` and the `braintrust[cli]` dev dep installed (`uv sync` handles this).

When you add a new local import in `evals.py` (or its transitive deps), add the file path to `EVAL_SOURCE_FILES` in the push script. The bundle is an explicit allow-list — auto-walk doesn't work reliably here (see the wrapper's header comment for why).


## Mustache variable contract

Bare `{{var}}` references are **required**: the dataset row must have a matching key (any value, including `null` or empty string). Missing → `ValueError` that names the offending prompt and variable.

To make a variable optional, use Mustache section syntax. Sections render nothing when the key is missing or falsy:

```
{{#city}}Looking for events in {{city}}.{{/city}}
{{#month}}Limit to {{month}}.{{/month}}
```

Extras in the dataset row (keys the prompts don't reference) are ignored. This is intentional — datasets often carry metadata fields the LLM doesn't need.

The stage 2 prompt has one extra variable available: `{{main_prompt_output}}` holds the raw text stage 1 produced. The strict-key check excludes it from row validation.

## One-time Braintrust project setup

In the `braintrust-eval-sandbox` Braintrust project, Project Settings → Environment Variables:

- `GEMINI_API_KEY` — required for the Gemini calls.
- `ENVIRONMENT=eval` — stamps every span with `metadata.environment="eval"` so eval rows are filterable in the Logs view.

## Limitations

- Model hardcoded to `gemini-3-flash-preview`. Per-prompt model picking via the prompt parameter's `options.model` isn't honored — the Gemini client uses its default model. If we need per-eval model selection later, expose a 5th parameter.
- No derived variables (`today`, `days_remaining`, etc). Whatever the prompt needs has to be in the dataset row.
- Stage 1 → stage 2 wiring is one direction: `main_prompt_output` only. If stage 2 needs the original row vars too, it gets them — every row key is available in both stages.
- No scorers shipped. Write them in the Braintrust UI for each use case.
