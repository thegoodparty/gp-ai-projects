"""Shared JSON Schema Draft-07 violation formatter.

Two surfaces validate Draft-07 schemas at runtime:

- Runner-side: `pmf_engine.runner.contract.collect_contract_errors` validates
  the agent's artifact against `output_schema`.
- Dispatch-side: `pmf_engine.control_plane.dispatch_handler.handler` validates
  the SQS message's `params` against `input_schema` before launching ECS.

Both need to render `jsonschema.ValidationError`s as `path: message` strings,
e.g. `segments[1].name: 99 is not of type 'string'`. Two formats means two
mental models for operators reading logs and agents reading
`validate_output.py` stdout. Keep one source of truth here.

Living here (`control_plane/`) — not `runner/` — because the Lambda zip is
the consumer that can't easily reach into the runner package; the runner can
import down into `control_plane` without circularity.
"""
from __future__ import annotations

from jsonschema import Draft7Validator


def format_validation_errors(validator: Draft7Validator, data) -> list[str]:
    """Run `validator` over `data` and return errors as `path: message` strings.

    Path format:
    - object keys joined by `.` (e.g. `district.state`)
    - array indices appended in brackets to the prior key (e.g. `items[0]`,
      `segments[1].name`, `score.dimensions[3].id`)
    - root-level errors render as `<root>`

    Errors are sorted by path so callers get deterministic ordering across
    runs. The sort key str-coerces every path element because
    `ValidationError.absolute_path` mixes strings (object keys) and ints
    (array indices); a naive `list(e.absolute_path)` sort raises TypeError on
    int<>str comparison and crashes the whole validation pass.
    """
    out: list[str] = []
    for err in sorted(
        validator.iter_errors(data),
        key=lambda e: [str(p) for p in e.absolute_path],
    ):
        path_parts: list[str] = []
        for part in err.absolute_path:
            if isinstance(part, int):
                if path_parts:
                    path_parts[-1] = f"{path_parts[-1]}[{part}]"
                else:
                    path_parts.append(f"[{part}]")
            else:
                path_parts.append(str(part))
        path_str = ".".join(path_parts) if path_parts else "<root>"
        out.append(f"{path_str}: {err.message}")
    return out
