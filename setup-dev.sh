#!/bin/bash
# Development Environment Setup Script
# This script sets up the development environment according to project rules

set -e

echo "🚀 Setting up development environment for Campaign Plan Generator"

# Check if uv is installed
if ! command -v uv &> /dev/null; then
    echo "❌ uv is not installed. Please install it first:"
    echo "   curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi

echo "✅ uv is installed"

# Sync dependencies
echo "📦 Installing dependencies..."
uv sync --extra dev

# Install pre-commit hooks
echo "🔧 Setting up pre-commit hooks..."
uv run pre-commit install

# Run initial quality check
echo "🔍 Running initial quality check..."
echo "Running ruff check..."
uv run ruff check . --fix || echo "⚠️ Some ruff issues remain (see CODEBASE_COMPLIANCE_REPORT.md)"

echo "Running ruff format..."
uv run ruff format .

echo "Running mypy..."
uv run mypy . || echo "⚠️ Some type issues remain (continuing...)"

echo "Running tests..."
uv run pytest --tb=short || echo "⚠️ Some tests failed (continuing...)"

echo ""
echo "🎉 Development environment setup complete!"
echo ""
echo "📋 Next steps:"
echo "   1. Copy .env.example to .env and fill in your API keys"
echo "   2. Review CODEBASE_COMPLIANCE_REPORT.md for remaining work"
echo "   3. Start developing with automatic quality checks!"
echo ""
echo "💡 Helpful commands:"
echo "   uv run ruff check --fix .  # Fix linting issues"
echo "   uv run pytest             # Run tests"
echo "   uv run api_wrapper:app     # Start the API server"
