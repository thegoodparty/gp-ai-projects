from typing import List

from shared.logger import get_logger
from .scanner import ProcessableTask
from .config import TAG_PROCESSORS


logger = get_logger(__name__)


class TaskProcessor:
    def process_tasks(self, tasks: List[ProcessableTask], dry_run: bool = True) -> None:
        if not tasks:
            logger.info("No tasks to process")
            return

        unprocessed = [t for t in tasks if not t.already_processed]
        
        if not unprocessed:
            logger.info("All tagged tasks have already been processed")
            return

        logger.info(f"Processing {len(unprocessed)} tasks (dry_run={dry_run})")

        for task in unprocessed:
            processor_name = TAG_PROCESSORS.get(task.tag, "unknown")
            
            if dry_run:
                logger.info(
                    f"[DRY RUN] Would trigger {processor_name} for: "
                    f"[{task.tag}] {task.task.name} (ID: {task.task.id})"
                )
            else:
                self._trigger_processor(task, processor_name)

    def _trigger_processor(self, task: ProcessableTask, processor_name: str) -> None:
        logger.info(
            f"[PLACEHOLDER] Triggering Fargate task '{processor_name}' for: "
            f"[{task.tag}] {task.task.name} (ID: {task.task.id})"
        )
