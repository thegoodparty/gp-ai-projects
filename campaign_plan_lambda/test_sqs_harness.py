"""
Local SQS harness for testing the Lambda handler end-to-end.

Real Gemini API calls are made (from local .env).
S3 writes go to a local file. SQS messages are printed to console.

Usage:
    source .venv/bin/activate
    python campaign_plan_lambda/test_sqs_harness.py
"""

import json
import os
import sys
import uuid
from pathlib import Path
from unittest.mock import MagicMock
from collections import defaultdict

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from dotenv import load_dotenv
load_dotenv(project_root / ".env")

sqs_message_body = {
    "campaignId": 99999,
    "election_date": "2026-11-04",
    "city": "Boston",
    "state": "MA",
}

lambda_event = {
    "Records": [
        {
            "messageId": str(uuid.uuid4()),
            "receiptHandle": "fake-receipt-handle",
            "body": json.dumps(sqs_message_body),
            "attributes": {
                "ApproximateReceiveCount": "1",
                "SentTimestamp": "1711324800000",
                "MessageGroupId": f"gp-queue-campaign-plan-{sqs_message_body['campaignId']}",
                "MessageDeduplicationId": str(uuid.uuid4()),
            },
            "messageAttributes": {},
            "md5OfBody": "fake-md5",
            "eventSource": "aws:sqs",
            "eventSourceARN": "arn:aws:sqs:us-west-2:333022194791:campaign-plan-input-dev.fifo",
            "awsRegion": "us-west-2",
        }
    ]
}

captured = defaultdict(list)


def mock_s3_put_object(**kwargs):
    captured["s3"].append(kwargs)
    body = kwargs.get("Body", "")
    output_path = Path(__file__).parent / "test_sqs_output.json"
    with open(output_path, "w") as f:
        f.write(body)
    print(f"\n  [MOCK S3] PUT s3://{kwargs['Bucket']}/{kwargs['Key']}")
    print(f"  [MOCK S3] Written locally to: {output_path}")
    return {"ResponseMetadata": {"HTTPStatusCode": 200}}


def mock_sqs_send_message(**kwargs):
    captured["sqs"].append(kwargs)
    body = json.loads(kwargs.get("MessageBody", "{}"))
    print(f"\n  [MOCK SQS] Message to: {kwargs.get('QueueUrl', 'unknown')}")
    print(f"  [MOCK SQS] Type: {body.get('type')}")
    print(f"  [MOCK SQS] Data: {json.dumps(body.get('data', {}), indent=4)}")
    return {"MessageId": str(uuid.uuid4())}


def main():
    print("=" * 60)
    print("Campaign Plan Lambda — SQS Harness Test")
    print("=" * 60)
    print()
    print("Input SQS message:")
    print(json.dumps(sqs_message_body, indent=2))
    print()

    os.environ["ENVIRONMENT"] = "dev"
    os.environ["S3_RESULTS_BUCKET"] = "campaign-plan-results-dev"
    os.environ["OUTPUT_SQS_QUEUE_URL"] = "https://sqs.us-west-2.amazonaws.com/333022194791/develop-Queue.fifo"

    mock_s3_client = MagicMock()
    mock_s3_client.put_object.side_effect = mock_s3_put_object

    mock_sqs_client = MagicMock()
    mock_sqs_client.send_message.side_effect = mock_sqs_send_message

    import campaign_plan_lambda.handler as handler_module
    import campaign_plan_lambda.output as output_module

    handler_module._secrets_cache = handler_module.Secrets(
        GEMINI_API_KEY=os.environ.get("GEMINI_API_KEY", ""),
    )

    output_module._s3_client = mock_s3_client
    output_module._sqs_client = mock_sqs_client

    print("Running handler...")
    print()

    try:
        handler_module.handler(lambda_event, None)
        print()
        print("=" * 60)
        print("SUCCESS")
        print("=" * 60)
    except Exception as e:
        print()
        print("=" * 60)
        print(f"FAILED: {e}")
        print("=" * 60)
        raise

    print()
    print(f"S3 puts: {len(captured['s3'])}")
    print(f"SQS messages: {len(captured['sqs'])}")

    if captured["s3"]:
        result = json.loads(captured["s3"][0]["Body"])
        print(f"\nTasks in output: {result.get('taskCount', 0)}")
        if result.get("tasks"):
            for t in result["tasks"]:
                print(f"  [{t['date']}] {t['title']}: {t['description']}")

    if captured["sqs"]:
        completion = json.loads(captured["sqs"][0]["MessageBody"])
        print(f"\nCompletion message:")
        print(json.dumps(completion, indent=2))


if __name__ == "__main__":
    main()
