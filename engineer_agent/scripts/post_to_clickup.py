import argparse
import sys

from shared.clickup_client import ClickUpClient
from engineer_agent.agent.config import BOT_PREFIX


def main():
    parser = argparse.ArgumentParser(description="Post a comment to a ClickUp task")
    parser.add_argument("--task-id", required=True, help="ClickUp task ID")
    parser.add_argument("--comment", required=True, help="Comment text to post")
    args = parser.parse_args()

    comment = args.comment
    if not comment.startswith(BOT_PREFIX):
        comment = f"{BOT_PREFIX} {comment}"

    try:
        client = ClickUpClient()
        client.create_task_comment(args.task_id, comment, notify_all=False)
        client.close()
        print(f"Successfully posted comment to task {args.task_id}")
    except Exception as e:
        print(f"Error posting to ClickUp: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
