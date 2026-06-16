import boto3


class DataQueryTracker:
    """Per-ticket counter of successful Databricks queries, backed by DynamoDB.

    The count is stamped onto the run's scope-ticket item (same table, same
    `pk`), so every broker instance reads and writes the same value. This keeps
    the broker stateless and horizontally scalable — the previous process-local
    counter silently broke once the service ran more than one task, because a
    run's Databricks query and its artifact publish land on different instances.

    Feeds the artifact_publish anti-fabrication gate: if a manifest declares
    `scope.allowed_tables` but the count is zero, the agent's data calls all
    failed (or it never queried), so the output is synthetic and the publish is
    rejected. The gate keys off scope, not a hardcoded experiment list, so the
    broker stays consumer-domain-agnostic.

    The counter rides the scope ticket's TTL, so it is cleaned up with the run.
    """

    def __init__(self, table_name: str, dynamodb_client=None) -> None:
        self._table_name = table_name
        self._client = dynamodb_client or boto3.client("dynamodb")

    def increment(self, ticket_pk: str) -> None:
        # Atomic server-side ADD: concurrent queries from the same run hitting
        # different broker instances cannot lose updates (no read-modify-write).
        self._client.update_item(
            TableName=self._table_name,
            Key={"pk": {"S": ticket_pk}},
            UpdateExpression="ADD query_count :one",
            ExpressionAttributeValues={":one": {"N": "1"}},
        )

    def get(self, ticket_pk: str) -> int:
        # Strongly consistent: the publish must observe increments that landed
        # on another instance moments earlier, or the gate misfires and rejects
        # a legitimately data-backed artifact.
        response = self._client.get_item(
            TableName=self._table_name,
            Key={"pk": {"S": ticket_pk}},
            ConsistentRead=True,
        )
        item = response.get("Item")
        if not item or "query_count" not in item:
            return 0
        return int(item["query_count"]["N"])

    def clear(self, ticket_pk: str) -> None:
        # Best-effort hygiene; the ticket item is deleted at end of run anyway.
        self._client.update_item(
            TableName=self._table_name,
            Key={"pk": {"S": ticket_pk}},
            UpdateExpression="REMOVE query_count",
        )
