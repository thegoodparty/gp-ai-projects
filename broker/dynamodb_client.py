import json
import time

import boto3
from botocore.exceptions import ClientError
from pydantic import BaseModel


class TicketAlreadyExistsError(Exception):
    pass


class ScopeTicket(BaseModel):
    pk: str
    run_id: str
    organization_slug: str
    experiment_id: str
    scope: dict
    params: dict
    exp: int
    issued_at: int
    issued_by: str
    prior_artifact_versions: dict[str, str] | None = None


class ScopeTicketStore:
    def __init__(self, table_name: str, dynamodb_client=None):
        self._table_name = table_name
        self._client = dynamodb_client or boto3.client("dynamodb")

    def put_ticket(self, ticket: ScopeTicket) -> None:
        item = {
            "pk": {"S": ticket.pk},
            "run_id": {"S": ticket.run_id},
            "organization_slug": {"S": ticket.organization_slug},
            "experiment_id": {"S": ticket.experiment_id},
            "scope": {"S": json.dumps(ticket.scope)},
            "params": {"S": json.dumps(ticket.params)},
            "exp": {"N": str(ticket.exp)},
            "issued_at": {"N": str(ticket.issued_at)},
            "issued_by": {"S": ticket.issued_by},
        }
        if ticket.prior_artifact_versions is not None:
            item["prior_artifact_versions"] = {"S": json.dumps(ticket.prior_artifact_versions)}

        now = int(time.time())
        try:
            self._client.put_item(
                TableName=self._table_name,
                Item=item,
                ConditionExpression="attribute_not_exists(pk) OR exp < :now",
                ExpressionAttributeValues={":now": {"N": str(now)}},
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise TicketAlreadyExistsError(f"Ticket already exists for pk={ticket.pk}") from e
            raise

    def get_ticket(self, broker_token: str) -> ScopeTicket | None:
        response = self._client.get_item(
            TableName=self._table_name,
            Key={"pk": {"S": broker_token}},
        )
        item = response.get("Item")
        if not item:
            return None

        exp = int(item["exp"]["N"])
        if exp <= int(time.time()):
            return None

        prior = None
        if "prior_artifact_versions" in item:
            prior = json.loads(item["prior_artifact_versions"]["S"])

        return ScopeTicket(
            pk=item["pk"]["S"],
            run_id=item["run_id"]["S"],
            organization_slug=item["organization_slug"]["S"],
            experiment_id=item["experiment_id"]["S"],
            scope=json.loads(item["scope"]["S"]),
            params=json.loads(item["params"]["S"]),
            exp=exp,
            issued_at=int(item["issued_at"]["N"]),
            issued_by=item["issued_by"]["S"],
            prior_artifact_versions=prior,
        )

    def delete_ticket(self, broker_token: str) -> None:
        self._client.delete_item(
            TableName=self._table_name,
            Key={"pk": {"S": broker_token}},
        )
