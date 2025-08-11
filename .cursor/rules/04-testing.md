# Testing Strategy

Framework:

- Use pytest.
- Organize tests mirroring package structure: `tests/<package>/test_<module>.py`.

Coverage:

- Aim for ≥85% on touched code. Add tests when fixing bugs or adding features.

Styles:

- Use AAA (Arrange-Act-Assert).
- Parametrize where meaningful.
- Avoid network in unit tests; mock external calls (e.g., `api_wrapper` HTTP).

Fixtures:

- Common fixtures in `tests/conftest.py`.
- Use `tmp_path` for file system interactions.
- Use `monkeypatch` to inject env vars and replace network layers.

Example (parametrized, typed):
import pytest

    @pytest.mark.parametrize("value,expected", [(2, 4), (3, 9)])
    def test_square(value: int, expected: int) -> None:
        assert square(value) == expected

For CLI:

- Test `main()` functions via `capsys` or `pytest`’s `CliRunner` if using typer/click.

When refactoring:

- First, add characterization tests around existing behavior (esp. `api_wrapper.py` and section scripts), then refactor.
