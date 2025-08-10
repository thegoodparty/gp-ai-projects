# Python Style and Patterns

General:

- Target the Python version declared in `.python-version` / `pyproject.toml`.
- Use type hints everywhere (PEP 484/PEP 561). No untyped public functions/classes.
- Prefer composition over inheritance; small, testable, single-responsibility units.
- Avoid global mutable state; use dependency injection for IO and clients.
- Use `pathlib` for paths; `subprocess.run(..., check=True)` for shelling out; `datetime` with timezone-aware `datetime` (UTC).

Formatting and linting (enforced via tooling):

- Format with Black-compatible settings (88 line length) and Ruff for lint+isort.
- No unused imports, no wildcard imports, no redefinitions.
- Avoid `print`; use logging (see logging rule).
- No bare `except:`. Catch specific exceptions. Re-raise with context.
- Limit cyclomatic complexity. Split large functions (>50 LOC) when reasonable.

Docstrings:

- Use Google-style docstrings for public symbols.
- Brief one-liner + Args/Returns/Raises/Examples where relevant.
- Keep docstrings accurate and updated when changing signatures.

APIs and IO:

- All network calls must have timeouts and sensible retries (idempotent operations only).
- Sanitize and redact secrets in any error message.
- Validate inputs early; fail fast with clear exceptions.

Naming:

- Modules and packages: snake_case. Classes: PascalCase. Functions/vars: snake_case.
- Tests mirror module names and structure.

Performance:

- Avoid premature optimization; prefer readability first. Consider caching (functools.lru_cache) for pure functions.

Examples:

- Prefer:
  def read_text(p: Path) -> str:
  return p.read_text(encoding="utf-8")

- Over:
  with open(str(path), "r") as f:
  return f.read()
