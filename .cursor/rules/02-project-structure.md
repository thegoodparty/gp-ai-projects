# Project Structure and Organization

Keep and improve the current structure:

- `ai_generated_campaign_plan/sections/` contains runnable scripts; each should:

  - Have a `main()` and `if __name__ == "__main__":` guard.
  - Parse CLI args with argparse/typer if applicable.
  - Delegate logic to importable functions under `shared/` (or a new `ai_generated_campaign_plan/core/`), not inline everything in `__main__`.

- `shared/` and `api_wrapper.py`:
  - Move reusable logic into modules under `shared/` (e.g., `shared/config.py`, `shared/logging.py`, `shared/http.py`).
  - `api_wrapper.py` should be thin, typed, handle timeouts/retries, and no secret logging.

Packaging and imports:

- Prefer relative imports within a package; absolute imports across packages.
- Add `__init__.py` where needed to make imports explicit and testable.

Config:

- Centralize config:
  - Read from env using `os.environ` or a small helper (optionally `python-dotenv` if already present).
  - Provide defaults where safe.
  - Do not read `.env` directly in library code; allow the runner to load it.

CLI ergonomics:

- Ensure commands are runnable via `uv run ...`.
- Provide helpful `--help` messages and safe defaults.

File IO:

- Use `Path` and explicit encodings.
- Avoid current-dir assumptions; allow passing output directories via args or config.
