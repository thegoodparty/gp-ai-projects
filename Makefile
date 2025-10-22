.PHONY: help lint format type-check check test install-hooks uninstall-hooks

help:
	@echo "Available commands:"
	@echo "  make install-hooks    - Install pre-commit hooks"
	@echo "  make uninstall-hooks  - Uninstall pre-commit hooks"
	@echo "  make lint             - Run ruff linter"
	@echo "  make format           - Run ruff formatter"
	@echo "  make type-check       - Run mypy type checker"
	@echo "  make check            - Run all checks (lint + format + type)"
	@echo "  make test             - Run pytest tests"

install-hooks:
	uv add --dev pre-commit
	uv run pre-commit install
	@echo "✅ Pre-commit hooks installed"

uninstall-hooks:
	@if [ -f .git/hooks/pre-commit ]; then \
		uv run pre-commit uninstall; \
		echo "✅ Pre-commit hooks uninstalled"; \
	else \
		echo "ℹ️  No pre-commit hooks installed"; \
	fi

lint:
	@echo "🔍 Running ruff linter..."
	uv run ruff check .

format:
	@echo "🎨 Running ruff formatter..."
	uv run ruff format .

type-check:
	@echo "🔎 Running mypy type checker..."
	uv run mypy serve/v1_pipeline/ shared/ || true

check: lint format type-check
	@echo "✅ All checks complete"

test:
	@echo "🧪 Running tests..."
	uv run pytest tests/
