import json
import logging

logger = logging.getLogger(__name__)


class CallbackSender:
    def __init__(self, sqs_client, queue_url: str):
        self.sqs_client = sqs_client
        self.queue_url = queue_url

    def send_result(
        self,
        run_id: str,
        organization_slug: str,
        experiment_id: str,
        status: str,
        artifact_key: str = "",
        artifact_bucket: str = "",
        duration_seconds: float = 0,
        cost_usd: float = 0,
        reason_code: str = "",
        detail: str = "",
        qa_verdict: dict | None = None,
    ):
        # Heterogeneous value types (str / float / dict), so type the inner
        # `data` dict explicitly — otherwise mypy infers a narrow value type
        # from the literal and rejects the conditional qaVerdict assignment.
        data: dict[str, object] = {
            "experimentId": experiment_id,
            "runId": run_id,
            "organizationSlug": organization_slug,
            "status": status,
            "artifactKey": artifact_key,
            "artifactBucket": artifact_bucket,
            "durationSeconds": duration_seconds,
            "costUsd": cost_usd,
            "reasonCode": reason_code,
            "detail": detail,
            # gp-api's queue consumer reads data.error to populate
            # ExperimentRun.error (the user-visible failure text). Keep
            # populated with detail; gp-api's new schema ignores the
            # structured detail/reasonCode/costUsd fields but they stay
            # on the wire for future gp-api consumption.
            "error": detail,
        }
        body = {"type": "agentExperimentResult", "data": data}
        # PMF QA gate (contract E, v1 observe-only): the verdict rides the
        # success callback only. The envelope key is camelCase to match the
        # other data.* keys; the verdict BODY is forwarded verbatim (the
        # broker keeps it opaque), so its snake_case contract-C shape is
        # preserved. Omit the key entirely when no gate ran (no qa folder, or
        # a pre-gate runner) so older messages parse byte-identically.
        if qa_verdict is not None:
            data["qaVerdict"] = qa_verdict
        if not self.queue_url:
            logger.info("callback skipped (no queue_url): %s %s", run_id, status)
            return
        try:
            self.sqs_client.send_message(
                QueueUrl=self.queue_url,
                MessageBody=json.dumps(body),
                MessageGroupId=run_id,
                MessageDeduplicationId=f"{run_id}-{status}",
            )
        except Exception:
            logger.exception(
                "callback SQS send failed run_id=%s status=%s queue=%s",
                run_id,
                status,
                self.queue_url,
            )
            raise
