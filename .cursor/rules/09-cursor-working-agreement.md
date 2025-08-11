# Cursor Working Agreement

When generating or modifying code, Cursor should:

1. Read the relevant files, `pyproject.toml`, and `.python-version` before proposing changes.
2. Plan the change in a short bullet list (high-level) and then implement.
3. For refactors, create characterization tests first if behavior is unclear.
4. Keep diffs minimal; prefer surgical edits over wide churn.
5. After changes, update or add:
   - Docstrings
   - Type hints
   - Tests
   - Changelog in PR description
6. Use `uv` commands in any run instructions.
7. Never output secrets or fabricate unknown values; if unknown, ask.

Output expectations in chats:

- Provide final, cleaned answers (no chain-of-thought). Show code diffs or complete files as needed.
- If a decision is opinionated, cite the rule that justifies it (by filename).
