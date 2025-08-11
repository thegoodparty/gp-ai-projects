# Commits, Branches, and PR Quality

Commits:

- Small, focused commits. Imperative tense: "Add ...", "Fix ...".
- Include tests adjusting to the change.
- Update docs and `.env.example` if config changes.

Branches:

- Use feature branches; reference issue/section context if applicable.

PRs:

- Provide context, screenshots/logs for CLI UX if relevant.
- Checklist:
  - [ ] Code formatted and linted
  - [ ] Types check cleanly
  - [ ] Tests added/updated and passing
  - [ ] No secrets in code or logs
  - [ ] Backwards compatible (or breaking changes documented)

Reviews:

- Prefer comments with concrete suggestions and references to rules in this folder.
