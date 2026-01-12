from dataclasses import dataclass
from typing import List, Optional

from shared.clickup_client import ClickUpClient, ClickUpTask
from shared.logger import get_logger
from .config import MONITORED_LISTS, BOT_PREFIX, MonitoredList
from .task_filter import TaskFilter


logger = get_logger(__name__)


@dataclass
class ProcessableTask:
    task: ClickUpTask
    tag: str
    list_info: MonitoredList
    already_processed: bool


@dataclass
class ScanResult:
    list_info: MonitoredList
    todo_count: int
    tagged_tasks: List[ProcessableTask]


class ClickUpScanner:
    def __init__(
        self,
        client: Optional[ClickUpClient] = None,
        task_filter: Optional[TaskFilter] = None,
        lists_to_scan: Optional[List[MonitoredList]] = None
    ):
        self.client = client or ClickUpClient()
        self.task_filter = task_filter or TaskFilter()
        self.lists_to_scan = lists_to_scan or MONITORED_LISTS

    def scan_list(self, list_info: MonitoredList) -> ScanResult:
        logger.info(f"Scanning list: {list_info.folder}/{list_info.name} (ID: {list_info.list_id})")
        
        tasks = self.client.get_all_tasks(
            list_id=list_info.list_id,
            statuses=["to do"],
            include_closed=False
        )
        
        logger.debug(f"Found {len(tasks)} to-do tasks in {list_info.name}")
        
        tagged_tasks: List[ProcessableTask] = []
        
        for task in tasks:
            matching_tag = self.task_filter.get_matching_tag(task)
            if matching_tag:
                comments = self.client.get_task_comments(task.id)
                already_processed = self.task_filter.is_processed(comments)
                
                tagged_tasks.append(ProcessableTask(
                    task=task,
                    tag=matching_tag,
                    list_info=list_info,
                    already_processed=already_processed
                ))
                
                status = "SKIP (already processed)" if already_processed else "WILL PROCESS"
                logger.debug(f"  [{matching_tag}] {task.name} → {status}")
        
        return ScanResult(
            list_info=list_info,
            todo_count=len(tasks),
            tagged_tasks=tagged_tasks
        )

    def scan_all_lists(self) -> List[ScanResult]:
        logger.info(f"Starting scan of {len(self.lists_to_scan)} lists (status: to do only)")
        
        results: List[ScanResult] = []
        
        for list_info in self.lists_to_scan:
            result = self.scan_list(list_info)
            results.append(result)
        
        return results

    def mark_as_processed(self, task_id: str, message: str) -> None:
        comment_text = f"{BOT_PREFIX} {message}"
        self.client.create_task_comment(task_id, comment_text, notify_all=False)
        logger.info(f"Marked task {task_id} as processed")

    def close(self):
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
