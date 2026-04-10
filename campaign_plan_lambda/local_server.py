"""
Local HTTP server for testing campaign plan generation with gp-api.

Accepts POST requests with {campaignId, election_date, city, state},
runs real Gemini API calls, writes results to dev S3, and sends
completion to your personal SQS queue.

Usage:
    source .venv/bin/activate
    python campaign_plan_lambda/local_server.py

Requires in .env:
    GEMINI_API_KEY
    BRAINTRUST_API_KEY (optional)
    OUTPUT_SQS_QUEUE_URL (your personal queue, e.g. Felicks_Queue.fifo)

In gp-api .env, set:
    CAMPAIGN_PLAN_LOCAL_URL=http://localhost:8089/generate
"""

import json
import os
import sys
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from threading import Thread

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from dotenv import load_dotenv
load_dotenv(project_root / ".env")

PORT = int(os.environ.get("LOCAL_SERVER_PORT", "8089"))

os.environ.setdefault("ENVIRONMENT", "local")
os.environ.setdefault("S3_RESULTS_BUCKET", "campaign-plan-results-dev")


def process_request(body: dict) -> None:
    """Run the Lambda handler with the given input, using real S3 and SQS."""
    import campaign_plan_lambda.handler as handler_module

    handler_module._secrets_cache = handler_module.Secrets(
        GEMINI_API_KEY=os.environ.get("GEMINI_API_KEY", ""),
        BRAINTRUST_API_KEY=os.environ.get("BRAINTRUST_API_KEY", ""),
    )

    lambda_event = {
        "Records": [
            {
                "messageId": str(uuid.uuid4()),
                "receiptHandle": "local-receipt",
                "body": json.dumps(body),
                "attributes": {
                    "ApproximateReceiveCount": "1",
                },
                "messageAttributes": {},
                "md5OfBody": "local",
                "eventSource": "aws:sqs",
                "eventSourceARN": "local",
                "awsRegion": "us-west-2",
            }
        ]
    }

    handler_module.handler(lambda_event, None)


class RequestHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(content_length))

        print(f"\n{'=' * 60}")
        print(f"Received request:")
        print(json.dumps(body, indent=2))
        print(f"{'=' * 60}\n")

        self.send_response(202)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"accepted": True}).encode())

        # Process in background so the HTTP response returns immediately
        thread = Thread(target=run_generation, args=(body,))
        thread.start()

    def log_message(self, format, *args):
        print(f"[HTTP] {args[0]}")


def run_generation(body: dict) -> None:
    try:
        process_request(body)
        print(f"\n{'=' * 60}")
        print("Generation completed successfully")
        print(f"{'=' * 60}\n")
    except Exception as e:
        print(f"\n{'=' * 60}")
        print(f"Generation FAILED: {e}")
        print(f"{'=' * 60}\n")


def main():
    output_queue = os.environ.get("OUTPUT_SQS_QUEUE_URL", "")
    print(f"Campaign Plan Local Server")
    print(f"  Port: {PORT}")
    print(f"  S3 Bucket: {os.environ.get('S3_RESULTS_BUCKET')}")
    print(f"  Output Queue: {output_queue or '(not set — completion messages will fail)'}")
    print(f"\nSet in gp-api .env:")
    print(f"  CAMPAIGN_PLAN_LOCAL_URL=http://localhost:{PORT}/generate")
    print(f"\nWaiting for requests...\n")

    server = HTTPServer(("localhost", PORT), RequestHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()


if __name__ == "__main__":
    main()
