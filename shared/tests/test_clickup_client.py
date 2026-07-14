"""Behavioral tests for ClickUpComment.get_text().

The fixtures mirror the REAL ClickUp GET /task/{id}/comment response shape,
captured live during the 2026-07-14 clickup_bot duplicate-launch incident
(task DATA-2108): plain text items carry NO "type" key ("type" appears only
on rich items such as "tag" mentions), and the full rendered text lives in a
top-level "comment_text" field. The previous matcher in clickup_bot filtered
items on type == "text" and therefore matched 0 real comments; this module
had the same bug in get_text(). Never add fields here that a live capture
does not show.
"""

from typing import Any

from shared.clickup_client import ClickUpComment

# Verbatim shape from the live capture (trimmed): a human comment mixing
# typed "tag" mention items with untyped plain-text items.
REAL_HUMAN_COMMENT: dict[str, Any] = {
    "id": "90130291057993",
    "comment": [
        {
            "type": "tag",
            "user": {"id": 105985351, "username": "Hugh Karimi"},
            "text": "@Hugh Karimi",
        },
        {"text": " BR's data is out of date here.", "attributes": {}},
        {"text": "\n", "attributes": {"block-id": "block-42H9Q2cBDM"}},
    ],
    "comment_text": "@Hugh Karimi BR's data is out of date here.\n",
    "user": {"id": 111932291, "username": "Chadwyck"},
    "date": "1784037977990",
    "reply_count": 0,
}

# Verbatim shape of the bot's own ack comment: a single item with ONLY a
# "text" key — no "type".
REAL_BOT_COMMENT: dict[str, Any] = {
    "id": "90130291038679",
    "comment": [{"text": "[GP-Bot] Processing started (analyze, model: opus)..."}],
    "comment_text": "[GP-Bot] Processing started (analyze, model: opus)...",
    "user": {"id": 105985359, "username": "Collin Park"},
    "date": "1784036421905",
    "reply_count": 0,
}


class TestGetText:
    def test_prefers_top_level_comment_text(self) -> None:
        comment = ClickUpComment(**REAL_BOT_COMMENT)
        assert comment.get_text() == "[GP-Bot] Processing started (analyze, model: opus)..."

    def test_concatenates_untyped_items_when_comment_text_absent(self) -> None:
        # The 2026-07-14 shape bug: real plain-text items have no "type" key,
        # so a type == "text" filter matches nothing and the text is lost.
        payload = {key: value for key, value in REAL_BOT_COMMENT.items() if key != "comment_text"}
        comment = ClickUpComment(**payload)
        assert comment.get_text() == "[GP-Bot] Processing started (analyze, model: opus)..."

    def test_mixed_typed_and_untyped_items_reconstruct_full_text(self) -> None:
        # Mention ("tag") items contribute their rendered text too — matching
        # what the top-level comment_text contains for the same comment.
        payload = {key: value for key, value in REAL_HUMAN_COMMENT.items() if key != "comment_text"}
        comment = ClickUpComment(**payload)
        assert comment.get_text() == "@Hugh Karimi BR's data is out of date here.\n"

    def test_forward_compat_type_text_items_still_match(self) -> None:
        # If ClickUp ever ADDS a "type": "text" key to plain items, their text
        # must still be included.
        payload: dict[str, Any] = {
            "id": "1",
            "comment": [{"type": "text", "text": "hello "}, {"text": "world"}],
        }
        comment = ClickUpComment(**payload)
        assert comment.get_text() == "hello world"

    def test_no_text_anywhere_returns_empty_string(self) -> None:
        comment = ClickUpComment(id="1")
        assert comment.get_text() == ""

    def test_text_content_alias_populates_comment_text(self) -> None:
        # Some ClickUp responses carry the text under "text_content"; the
        # model aliases it onto comment_text and get_text() must honor it.
        # Direct keyword call (alias keywords are valid __init__ params on a
        # pydantic v2 model), NOT an inline **{...} unpack: mypy infers an
        # all-str dict literal as dict[str, str], which strict mode rejects
        # against the model's typed params.
        comment = ClickUpComment(id="1", text_content="aliased text")
        assert comment.get_text() == "aliased text"

    def test_null_text_items_contribute_nothing(self) -> None:
        # A null "text" value must neither crash nor inject the string "None"
        # into the reconstructed text — a "None" prefix would corrupt
        # prefix-matched markers like "[GP-Bot] Processing started".
        payload: dict[str, Any] = {
            "id": "1",
            "comment": [{"text": None}, {"text": "[GP-Bot] Processing started"}],
        }
        comment = ClickUpComment(**payload)
        assert comment.get_text() == "[GP-Bot] Processing started"

    def test_items_without_text_keys_are_skipped(self) -> None:
        # Rich items (images, attachments) may carry no "text" at all; they
        # must contribute nothing rather than crash.
        payload: dict[str, Any] = {
            "id": "1",
            "comment": [{"type": "attachment"}, {"text": "caption"}],
        }
        comment = ClickUpComment(**payload)
        assert comment.get_text() == "caption"
