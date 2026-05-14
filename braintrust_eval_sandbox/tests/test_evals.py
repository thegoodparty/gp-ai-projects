"""Unit tests for braintrust_eval_sandbox/evals.py.

Focused coverage for the three non-trivial pure helpers — the ones that
enforce correctness guarantees the README and parameter descriptions
promise to PMs:

  - `_normalize_schema_for_gemini`: rewrites JSON Schema to Gemini's
    structured-output dialect, raises clear errors for unsupported shapes.
  - `_require_keys` (+ the regex helpers it leans on): strict Mustache
    variable validation against dataset row keys, with Mustache section
    syntax as the escape hatch for optional vars.
  - `_extract_template_text`: pulls raw template text out of a Braintrust
    Prompt object before rendering, so the strict-key check sees what the
    PM wrote rather than the already-substituted output.

Also a regression test on `EVAL_PARAMETERS` shape — same null-field-in-
playground-validator failure mode the campaign_plan_lambda sibling tests
lock down.
"""

import copy
import importlib
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest


def _import_evals():
    """Import braintrust_eval_sandbox.evals without triggering the module-
    level `Eval(...)` call (which connects to Braintrust). Mirrors the
    pattern in campaign_plan_lambda/tests/test_evals.py — patches `Eval`
    and `init_dataset` so the import doesn't authenticate. If a prior test
    or `push_eval_to_braintrust.sh` already imported the module, reuse it."""
    if "braintrust_eval_sandbox.evals" in sys.modules:
        return sys.modules["braintrust_eval_sandbox.evals"]
    with patch("braintrust.Eval"), patch("braintrust.init_dataset"):
        return importlib.import_module("braintrust_eval_sandbox.evals")


evals = _import_evals()


class TestNormalizeSchemaNullable:
    """The reason this helper exists: standard JSON Schema's nullable form
    is a type union (`["X", "null"]`), but Gemini's SDK only accepts a
    singular `type` plus a separate `nullable` flag. The walker rewrites
    the former into the latter so PMs can paste OpenAI/Anthropic-flavor
    schemas without manual translation."""

    def test_nullable_union_rewrites(self):
        result = evals._normalize_schema_for_gemini({"type": ["string", "null"]})
        assert result == {"type": "string", "nullable": True}

    def test_nullable_union_order_independent(self):
        # Mustache-style schemas don't promise null comes last.
        result = evals._normalize_schema_for_gemini({"type": ["null", "number"]})
        assert result == {"type": "number", "nullable": True}

    def test_single_element_list_unwraps(self):
        # Some schema generators emit `["string"]` instead of `"string"`.
        # Gemini rejects the list outright; treat it as the scalar form.
        result = evals._normalize_schema_for_gemini({"type": ["integer"]})
        assert result == {"type": "integer"}

    def test_scalar_type_passes_through(self):
        assert evals._normalize_schema_for_gemini({"type": "string"}) == {"type": "string"}

    def test_already_gemini_form_passes_through(self):
        already = {"type": "string", "nullable": True}
        assert evals._normalize_schema_for_gemini(already) == already

    def test_nullable_propagates_recursively(self):
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "score": {"type": ["number", "null"]},
            },
            "required": ["name"],
        }
        result = evals._normalize_schema_for_gemini(schema)
        assert result["properties"]["score"] == {"type": "number", "nullable": True}
        # Non-nullable siblings untouched.
        assert result["properties"]["name"] == {"type": "string"}
        # Top-level keys preserved.
        assert result["type"] == "object"
        assert result["required"] == ["name"]

    def test_nullable_propagates_through_array_items(self):
        # The walker recurses into every dict value, so `items` (a sibling of
        # `properties` for array-typed nodes) must rewrite just like
        # properties does. If a future refactor special-cases `properties`,
        # this test catches the regression.
        schema = {
            "type": "array",
            "items": {"type": ["string", "null"]},
        }
        assert evals._normalize_schema_for_gemini(schema) == {
            "type": "array",
            "items": {"type": "string", "nullable": True},
        }

    def test_does_not_mutate_input(self):
        # The walker is supposed to be pure: build new dicts/lists rather
        # than mutate. If someone optimizes for memory by mutating in place,
        # PMs holding a reference to the original schema would see it change
        # under them. Deepcopy-then-compare locks the contract.
        original = {
            "type": "object",
            "properties": {
                "score": {"type": ["number", "null"]},
                "tags": {"type": "array", "items": {"type": ["string", "null"]}},
            },
        }
        snapshot = copy.deepcopy(original)
        evals._normalize_schema_for_gemini(original)
        assert original == snapshot, "input schema was mutated"


class TestNormalizeSchemaRejectsBadShapes:
    """Better to raise here with a named message than let Gemini's
    pydantic-deep traceback surface to the PM."""

    def test_type_union_raises(self):
        with pytest.raises(ValueError, match="type unions"):
            evals._normalize_schema_for_gemini({"type": ["string", "number"]})

    def test_bare_null_list_raises(self):
        # `["null"]` is neither a nullable union nor a real union — just an
        # empty-information schema. Force the PM to make it explicit.
        with pytest.raises(ValueError, match="bare null"):
            evals._normalize_schema_for_gemini({"type": ["null"]})

    def test_ref_raises(self):
        with pytest.raises(ValueError, match=r"\$ref"):
            evals._normalize_schema_for_gemini({"$ref": "#/$defs/Foo"})

    def test_defs_raises(self):
        with pytest.raises(ValueError, match=r"\$defs"):
            evals._normalize_schema_for_gemini(
                {"$defs": {"Foo": {"type": "object"}}, "type": "object"}
            )

    def test_definitions_raises(self):
        # Older JSON Schema dialects use `definitions` instead of `$defs`.
        # Same problem, same error.
        with pytest.raises(ValueError, match="definitions"):
            evals._normalize_schema_for_gemini(
                {"definitions": {"Foo": {"type": "object"}}, "type": "object"}
            )

    def test_nested_ref_raises(self):
        # `$ref` buried under `properties` still fires, because the walker
        # checks at every recursion level. Common in Pydantic-derived
        # schemas where nested objects become `$ref` -> `$defs` entries.
        with pytest.raises(ValueError, match=r"\$ref"):
            evals._normalize_schema_for_gemini({
                "type": "object",
                "properties": {"home": {"$ref": "#/$defs/Address"}},
            })

    def test_empty_type_list_raises(self):
        # `{"type": []}` provides no type information. Without the explicit
        # raise this falls past all four nullable/union branches and reaches
        # Gemini's SDK as a list, surfacing an opaque pydantic validation
        # error — exactly what the normalizer is here to prevent.
        with pytest.raises(ValueError, match="empty"):
            evals._normalize_schema_for_gemini({"type": []})


class TestRequireKeysStrict:
    def test_all_present_no_raise(self):
        # `_require_keys` returns None on success. Asserting that — rather
        # than leaving the test as a bare call — locks the silent-success
        # path so a future refactor to "return None always" doesn't pass.
        result = evals._require_keys(
            "Hello {{city}} on {{date}}.",
            row_keys={"city", "date"},
            label="main_prompt",
            reserved=set(),
        )
        assert result is None

    def test_missing_var_raises(self):
        with pytest.raises(ValueError) as exc:
            evals._require_keys(
                "Hello {{city}} on {{date}}.",
                row_keys={"city"},
                label="main_prompt",
                reserved=set(),
            )
        msg = str(exc.value)
        assert "main_prompt" in msg
        assert "date" in msg
        # Helpful error: lists what the row actually had.
        assert "city" in msg

    def test_multiple_missing_listed(self):
        with pytest.raises(ValueError) as exc:
            evals._require_keys(
                "{{foo}} {{bar}} {{baz}}",
                row_keys={"foo"},
                label="x",
                reserved=set(),
            )
        msg = str(exc.value)
        assert "bar" in msg
        assert "baz" in msg

    def test_reserved_var_excluded(self):
        # The stage-2 prompt may reference `{{main_prompt_output}}` even
        # though it isn't in the row — the task supplies it. Should not
        # trigger missing-key, and should return None to lock the
        # silent-success branch.
        result = evals._require_keys(
            "Extract from: {{main_prompt_output}}",
            row_keys=set(),
            label="structured_output_prompt",
            reserved={"main_prompt_output"},
        )
        assert result is None

    def test_section_makes_var_optional(self):
        # `{{#feliks}}...{{/feliks}}` declares feliks as a section.
        # Per the contract, that makes feliks optional everywhere.
        result = evals._require_keys(
            "{{#feliks}}note: {{feliks}}.{{/feliks}}",
            row_keys=set(),
            label="main_prompt",
            reserved=set(),
        )
        assert result is None

    def test_inverted_section_makes_var_optional(self):
        # Same exemption for `{{^var}}...{{/var}}` (renders when missing).
        result = evals._require_keys(
            "{{^city}}no city given{{/city}}",
            row_keys=set(),
            label="main_prompt",
            reserved=set(),
        )
        assert result is None

    def test_section_does_not_exempt_other_required_vars(self):
        # A section around `feliks` shouldn't excuse a missing `city`.
        with pytest.raises(ValueError, match="city"):
            evals._require_keys(
                "{{city}} {{#feliks}}{{feliks}}{{/feliks}}",
                row_keys=set(),
                label="main_prompt",
                reserved=set(),
            )

    def test_extra_row_keys_ignored(self):
        # Datasets carry metadata fields the prompts don't reference;
        # those should not trigger any error.
        result = evals._require_keys(
            "Hello {{name}}.",
            row_keys={"name", "notes", "dataset_version", "created_by"},
            label="x",
            reserved=set(),
        )
        assert result is None


class TestBareAndSectionVarRegexes:
    """The two regexes are the foundation of the strict-key check. Lock
    their behavior directly so a future tweak that breaks them is loud."""

    def test_bare_var_matches_simple(self):
        assert evals._bare_vars("hello {{name}}") == {"name"}

    def test_bare_var_ignores_sigils(self):
        # Section openers/closers, inverted sections, comments, partials,
        # unescaped — none of these are "bare" references.
        template = "{{#a}}{{/a}} {{^b}}{{/b}} {{!c}} {{>d}} {{&e}}"
        assert evals._bare_vars(template) == set()

    def test_bare_var_handles_whitespace_inside_braces(self):
        # Mustache allows `{{  name  }}` — our regex must too.
        assert evals._bare_vars("{{  name  }}") == {"name"}

    def test_section_var_matches_section_opener(self):
        assert evals._section_vars("{{#feliks}}...{{/feliks}}") == {"feliks"}

    def test_section_var_matches_inverted_section(self):
        assert evals._section_vars("{{^missing}}...{{/missing}}") == {"missing"}

    def test_section_var_ignores_bare_refs(self):
        assert evals._section_vars("{{just_a_var}}") == set()


def _completion_prompt(content: str) -> SimpleNamespace:
    """Mock a Braintrust completion-type Prompt. The function only needs
    `prompt.type` and `prompt.content`, so a duck-typed namespace suffices.
    """
    return SimpleNamespace(
        prompt=SimpleNamespace(type="completion", content=content),
    )


def _chat_prompt(*messages) -> SimpleNamespace:
    """Mock a Braintrust chat-type Prompt. Each arg is a message namespace
    or dict; the function reads `.content` (or `["content"]` if dict)."""
    return SimpleNamespace(
        prompt=SimpleNamespace(type="chat", messages=list(messages)),
    )


def _msg(content) -> SimpleNamespace:
    return SimpleNamespace(content=content)


class TestExtractTemplateText:
    def test_completion_returns_content(self):
        assert evals._extract_template_text(_completion_prompt("hi {{x}}")) == "hi {{x}}"

    def test_chat_with_string_content_joins_messages(self):
        prompt = _chat_prompt(
            _msg("system: {{role}}"),
            _msg("user: hello {{name}}"),
        )
        text = evals._extract_template_text(prompt)
        # Joined with newlines so all template vars are visible to the
        # bare-var regex regardless of which message they live in.
        assert "{{role}}" in text
        assert "{{name}}" in text

    def test_chat_with_dict_pieces_picks_up_text(self):
        # JSON-on-the-wire form: each message's content is a list of
        # `{"type": "text", "text": "..."}` dicts (and optionally image
        # parts). The strict-key check would silently bypass these if the
        # extractor didn't handle the dict shape.
        prompt = _chat_prompt(
            _msg([
                {"type": "text", "text": "first {{a}}"},
                {"type": "image_url", "image_url": {"url": "..."}},
                {"type": "text", "text": "second {{b}}"},
            ])
        )
        text = evals._extract_template_text(prompt)
        assert "{{a}}" in text
        assert "{{b}}" in text
        # Image parts have no `.text` and should be skipped, not crashed on.
        assert "image_url" not in text

    def test_chat_with_dataclass_like_pieces(self):
        # Dataclass form (Braintrust SDK's TextPart). Use namespaces as
        # stand-ins so we don't pull in the real types module.
        prompt = _chat_prompt(
            _msg([
                SimpleNamespace(type="text", text="hi {{a}}"),
                SimpleNamespace(type="image_url", image_url={"url": "..."}),
                SimpleNamespace(type="text", text="bye {{b}}"),
            ])
        )
        text = evals._extract_template_text(prompt)
        assert "{{a}}" in text
        assert "{{b}}" in text

    def test_chat_with_mixed_piece_shapes(self):
        # A single message with one of each shape — bare string, dict,
        # dataclass — all three must be picked up.
        prompt = _chat_prompt(
            _msg([
                "plain {{a}}",
                {"type": "text", "text": "dict {{b}}"},
                SimpleNamespace(type="text", text="dataclass {{c}}"),
            ])
        )
        text = evals._extract_template_text(prompt)
        for v in ("{{a}}", "{{b}}", "{{c}}"):
            assert v in text

    def test_none_prompt_returns_empty_string(self):
        # Defensive: shouldn't crash if `.prompt` is missing entirely.
        assert evals._extract_template_text(SimpleNamespace(prompt=None)) == ""

    def test_unknown_prompt_type_returns_empty(self):
        # Future-proofing: if Braintrust adds a new prompt block type the
        # function doesn't know yet, return empty rather than guessing.
        unknown = SimpleNamespace(prompt=SimpleNamespace(type="something_new"))
        assert evals._extract_template_text(unknown) == ""


def _walk(value, path=""):
    """Yield (jsonpath, leaf) for every node in a nested structure. Used
    by the EVAL_PARAMETERS no-null check to point at the exact field that
    broke if the test ever fails."""
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
    """Same regression check the campaign_plan_lambda eval has. The
    Braintrust playground silently rejects parameter shapes containing
    JSON `null` leaves anywhere in the dict-literal parts. Pydantic
    parameter classes are exempt because Braintrust serializes them via a
    different path; we only police the dict-literal entries here."""

    def test_dict_literal_params_have_no_null_anywhere(self):
        dict_only = {
            name: spec
            for name, spec in evals.EVAL_PARAMETERS.items()
            if isinstance(spec, dict)
        }
        for path, value in _walk(dict_only):
            assert value is not None, (
                f"EVAL_PARAMETERS contains None at {path}; the Braintrust "
                f"playground will hide the sandbox from the '+ Task → "
                f"Remote eval' picker. Omit the key entirely instead."
            )

    def test_all_four_params_present(self):
        # Cheap sanity: the four-parameter contract is the API of this
        # sandbox. If someone renames or drops one, that's a deliberate
        # change and this test should be updated alongside it.
        assert set(evals.EVAL_PARAMETERS) == {
            "main_prompt_search_enabled",
            "main_prompt",
            "structured_output_prompt",
            "structured_output_schema",
        }


class TestPipelineTaskRowCollision:
    """Stage 2 injects stage 1's output into the structured-output prompt
    under the reserved key `main_prompt_output`. If a dataset row supplies
    a column with that exact name, the inject would silently shadow the
    PM's data. The collision check runs at the very top of pipeline_task,
    before any LLM work, so this test can exercise it without mocking the
    Braintrust span machinery or constructing a real Gemini client."""

    def test_row_collision_with_reserved_var_raises(self):
        # Hooks isn't accessed because the collision check raises before
        # `init_braintrust` runs. A bare namespace is enough.
        hooks = SimpleNamespace()
        with pytest.raises(ValueError, match="main_prompt_output"):
            evals.pipeline_task(
                {"city": "Boston", "main_prompt_output": "would be shadowed"},
                hooks,
            )

    def test_normal_row_does_not_raise_collision(self):
        # Sanity: a row WITHOUT the reserved key must not trip the check.
        # We only verify the collision branch is skipped — full execution
        # would need a real Gemini client and Braintrust hooks, which is
        # explicitly out of scope for these unit tests. The cheapest signal
        # is that the function progresses past the check; we let it raise
        # AttributeError on `hooks.parameters` and assert we got that far.
        hooks = SimpleNamespace()
        with pytest.raises(AttributeError, match="parameters"):
            evals.pipeline_task({"city": "Boston"}, hooks)
