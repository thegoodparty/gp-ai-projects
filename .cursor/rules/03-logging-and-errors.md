# Logging, Error Handling, and Observability

Logging:

- Use the stdlib `logging` module.
- Create `shared/logging.py` with `get_logger(__name__)` and an init function:

  - Development: human-readable format.
  - Production (ENVIRONMENT=production): JSON lines (key=value or json) with ISO timestamps.
  - Do not duplicate handlers; make init idempotent.

- Log levels:
  - DEBUG: detailed developer info (no secrets).
  - INFO: high-level events.
  - WARNING: recoverable issues.
  - ERROR: failures that prevent operation of a unit of work.
  - CRITICAL: system-wide failure.

Secrets:

- Never log API keys, tokens, or full request/response if it contains sensitive data. Redact with `***`.

Errors:

- Prefer precise custom exceptions for domain errors (e.g., `ApiError`, `ConfigError`).
- Wrap external failures with context, e.g.:
  raise ApiError(f"Request failed (status={resp.status_code})") from exc

- Do not swallow exceptions. Either handle and continue (with a log) or re-raise.
- For CLI scripts, exit with non-zero code on failure and a clear message.
