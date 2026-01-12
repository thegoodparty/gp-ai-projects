from typing import List, Optional

from shared.clickup_client import ClickUpTask, ClickUpComment
from .config import TARGET_TAGS, BOT_PREFIX


class TaskFilter:
    def __init__(self, target_tags: Optional[List[str]] = None, bot_prefix: Optional[str] = None):
        self.target_tags = target_tags or TARGET_TAGS
        self.bot_prefix = bot_prefix or BOT_PREFIX

    def get_matching_tag(self, task: ClickUpTask) -> Optional[str]:
        if not task.tags:
            return None
        
        for tag in task.tags:
            tag_name = tag.get("name", "") if isinstance(tag, dict) else str(tag)
            if tag_name.lower() in [t.lower() for t in self.target_tags]:
                return tag_name
        
        return None

    def is_processed(self, comments: List[ClickUpComment]) -> bool:
        for comment in comments:
            text = comment.get_text()
            if text and text.startswith(self.bot_prefix):
                return True
        return False

    def get_bot_comment(self, comments: List[ClickUpComment]) -> Optional[ClickUpComment]:
        for comment in comments:
            text = comment.get_text()
            if text and text.startswith(self.bot_prefix):
                return comment
        return None
