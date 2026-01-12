import argparse
import sys
from typing import List

sys.path.insert(0, "/Users/collinpark/work/gp-ai-projects")

from shared.logger import get_logger
from clickup_monitor.monitor import (
    ClickUpScanner,
    TaskProcessor,
    ScanResult,
    MONITORED_LISTS,
    MonitoredList,
)


logger = get_logger(__name__)


def print_results(results: List[ScanResult]) -> dict:
    total_todo = 0
    total_tagged = 0
    total_processed = 0
    total_ready = 0

    print("\n" + "=" * 50)
    print("ClickUp Task Scanner")
    print(f"Scanning {len(results)} lists (status: to do only)")
    print("=" * 50)

    for result in results:
        total_todo += result.todo_count
        list_tagged = len(result.tagged_tasks)
        total_tagged += list_tagged

        print(f"\n[{result.list_info.folder}/{result.list_info.name}] ({result.todo_count} to-do tasks)")

        if result.tagged_tasks:
            print(f"  Found {list_tagged} tasks with target tags:")
            for pt in result.tagged_tasks:
                if pt.already_processed:
                    total_processed += 1
                    status = "SKIP"
                else:
                    total_ready += 1
                    status = "WILL PROCESS"
                print(f"    - [{pt.tag}] \"{pt.task.name}\" (ID: {pt.task.id})")
                print(f"      Already processed: {'Yes' if pt.already_processed else 'No'} → {status}")
        else:
            print("  No tasks with target tags")

    print("\n" + "-" * 50)
    print("Summary:")
    print(f"  Lists scanned: {len(results)}")
    print(f"  To-do tasks checked: {total_todo}")
    print(f"  Tasks with target tags: {total_tagged}")
    print(f"  Already processed: {total_processed}")
    print(f"  Ready to process: {total_ready}")
    print("-" * 50 + "\n")

    return {
        "lists_scanned": len(results),
        "todo_checked": total_todo,
        "tagged": total_tagged,
        "processed": total_processed,
        "ready": total_ready,
    }


def main():
    parser = argparse.ArgumentParser(description="Scan ClickUp lists for tagged tasks")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Print findings without processing (default: True)"
    )
    parser.add_argument(
        "--process",
        action="store_true",
        help="Actually process tasks (adds [GP-Bot] comments)"
    )
    parser.add_argument(
        "--list-id",
        type=str,
        help="Scan only a specific list by ID"
    )
    parser.add_argument(
        "--task-id",
        type=str,
        help="Mark a specific task as processed (use with --mark-processed)"
    )
    parser.add_argument(
        "--mark-processed",
        action="store_true",
        help="Mark a task as processed (requires --task-id)"
    )

    args = parser.parse_args()

    if args.mark_processed:
        if not args.task_id:
            print("Error: --mark-processed requires --task-id")
            sys.exit(1)
        
        with ClickUpScanner() as scanner:
            scanner.mark_as_processed(args.task_id, "Manually marked as processed")
            print(f"Marked task {args.task_id} as processed")
        return

    lists_to_scan = MONITORED_LISTS
    if args.list_id:
        lists_to_scan = [
            MonitoredList(list_id=args.list_id, name="Custom", folder="Custom")
        ]

    with ClickUpScanner(lists_to_scan=lists_to_scan) as scanner:
        results = scanner.scan_all_lists()
        summary = print_results(results)

        if args.process and not args.dry_run:
            all_tasks = []
            for result in results:
                all_tasks.extend(result.tagged_tasks)
            
            processor = TaskProcessor()
            processor.process_tasks(all_tasks, dry_run=False)
        elif summary["ready"] > 0:
            print("Run with --process to process these tasks")


if __name__ == "__main__":
    main()
