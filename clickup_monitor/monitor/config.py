from dataclasses import dataclass
from typing import Dict, List

BOT_PREFIX = "[GP-Bot]"

TARGET_TAGS = ["production-bug", "simple-task"]

@dataclass
class MonitoredList:
    list_id: str
    name: str
    folder: str

MONITORED_LISTS: List[MonitoredList] = [
    MonitoredList(
        list_id="901320540273",
        name="List",
        folder="Engineering/Bugs"
    ),
    MonitoredList(
        list_id="901321855604",
        name="Blocked States",
        folder="Engineering/Bugs"
    ),
    MonitoredList(
        list_id="901321761872",
        name="Bugs",
        folder="Engineering/Win"
    ),
    MonitoredList(
        list_id="901318405462",
        name="Backlog",
        folder="Engineering/Serve"
    ),
    MonitoredList(
        list_id="901321495230",
        name="List",
        folder="Engineering/Platform"
    ),
]

TAG_PROCESSORS: Dict[str, str] = {
    "production-bug": "bug_processor",
    "simple-task": "simple_task_processor",
}
