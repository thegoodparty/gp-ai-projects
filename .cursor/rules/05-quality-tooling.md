# Quality Tooling and Automation

Linters/Formatters:

- Use Ruff for linting and import sorting, Black-compatible formatting. Configure in `pyproject.toml`.
- Enable error codes: E, F, W, I, N, UP, ANN, B, C4, T20, PYI, ARG, PLR (tune if needed).
- Enforce type hints (ANN) on public APIs.

Type checking:

- Use mypy (or pyright if preferred). Configure strictness incrementally (start with `warn-redundant-casts`, `warn-unused-ignores`, `disallow-incomplete-defs`).

Pre-commit:

- Add a `.pre-commit-config.yaml` with hooks: ruff, black (or just ruff format), mypy, trailing-whitespace, end-of-file-fixer.

CI:

- Add GitHub Actions workflow using `uv`:
  - `uv sync`
  - `uv run ruff check .`
  - `uv run ruff format --check .`
  - `uv run mypy .`
  - `uv run pytest -q --maxfail=1 --disable-warnings`

Dependency hygiene:

- Pin via `pyproject.toml`. Use `uv lock` to update.
- If adding deps, justify in PR and keep them minimal.

Developer UX:

- Provide `make` or `uv run` task aliases for common commands (lint, test, format).
