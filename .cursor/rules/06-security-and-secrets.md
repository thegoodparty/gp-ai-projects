# Security and Secrets

Environment:

- Load secrets from environment variables. Optionally support `.env` via bootstrap (never inside library code).
- Never commit `.env`; keep `.env.example` updated with placeholders and comments.

HTTP and external APIs:

- Always set timeouts (connect/read). Prefer `requests`/`httpx` with retry/backoff for idempotent GET/POST where appropriate.
- Validate and sanitize inputs/outputs.
- Do not print or log full payloads containing secrets or PII.

Filesystem:

- Validate user-provided paths; prevent directory traversal when writing files.
- Use least privilege when creating files (mode 0o600 if secrets).

General:

- Avoid executing arbitrary shell; if needed, pass args as lists and avoid `shell=True`.

Reviews:

- Flag any code that could leak secrets in logs, exceptions, or cache files (e.g., `log.txt`).
