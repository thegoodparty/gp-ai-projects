"""
body_validation.py — Re-export of body validation from top-level module.

Clean import path: `from meeting_pipeline.shared.body_validation import score_body_match`
"""

from meeting_pipeline.body_validation import (  # noqa: F401
    score_body_match,
    best_body_match,
    validate_body_for_city,
    REJECT_KEYWORDS,
    GOVERNING_KEYWORDS,
    VALIDATABLE_PLATFORMS,
)
