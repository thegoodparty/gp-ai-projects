import json
import time

import boto3
from botocore.exceptions import ClientError
from pydantic import BaseModel, Field


RUN_LOCK_PK_PREFIX = "run:"


def _run_lock_pk(run_id: str) -> str:
    return f"{RUN_LOCK_PK_PREFIX}{run_id}"


class TicketAlreadyExistsError(Exception):
    pass


class InputFileRef(BaseModel):
    bucket: str = Field(..., min_length=1, max_length=255)
    key: str = Field(..., min_length=1, max_length=1024)
    # Path-traversal defense: dest is written under /workspace/input/<dest>,
    # so it must be a simple filename — no separators, no parent refs, no
    # leading dot (which would mark a hidden file the agent's directory
    # walks could miss).
    dest: str = Field(..., pattern=r"^[A-Za-z0-9_][A-Za-z0-9._-]*$", max_length=255)


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
    clerk_user_id: str | None = None
    input_files: list[InputFileRef] | None = None


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
        if ticket.clerk_user_id is not None:
            item["clerk_user_id"] = {"S": ticket.clerk_user_id}
        if ticket.input_files is not None:
            item["input_files"] = {
                "S": json.dumps([f.model_dump() for f in ticket.input_files])
            }

        run_lock_item = {
            "pk": {"S": _run_lock_pk(ticket.run_id)},
            "run_id": {"S": ticket.run_id},
            "broker_token": {"S": ticket.pk},
            "exp": {"N": str(ticket.exp)},
        }

        now = int(time.time())
        try:
            self._client.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self._table_name,
                            "Item": item,
                            "ConditionExpression": "attribute_not_exists(pk) OR exp < :now",
                            "ExpressionAttributeValues": {":now": {"N": str(now)}},
                        }
                    },
                    {
                        "Put": {
                            "TableName": self._table_name,
                            "Item": run_lock_item,
                            "ConditionExpression": "attribute_not_exists(pk) OR exp < :now",
                            "ExpressionAttributeValues": {":now": {"N": str(now)}},
                        }
                    },
                ]
            )
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code in ("TransactionCanceledException", "ConditionalCheckFailedException"):
                raise TicketAlreadyExistsError(
                    f"Ticket already exists for pk={ticket.pk} or run_id={ticket.run_id}"
                ) from e
            raise

    def get_ticket(self, broker_token: str) -> ScopeTicket | None:
        if broker_token.startswith(RUN_LOCK_PK_PREFIX):
            return None
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

        clerk_user_id = None
        if "clerk_user_id" in item:
            clerk_user_id = item["clerk_user_id"]["S"]

        input_files = None
        if "input_files" in item:
            input_files = [
                InputFileRef(**f) for f in json.loads(item["input_files"]["S"])
            ]

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
            clerk_user_id=clerk_user_id,
            input_files=input_files,
        )

    def delete_ticket(self, broker_token: str) -> None:
        self._client.delete_item(
            TableName=self._table_name,
            Key={"pk": {"S": broker_token}},
        )

    def delete_ticket_and_run_lock(self, broker_token: str, run_id: str) -> None:
        self._client.transact_write_items(
            TransactItems=[
                {
                    "Delete": {
                        "TableName": self._table_name,
                        "Key": {"pk": {"S": broker_token}},
                    }
                },
                {
                    "Delete": {
                        "TableName": self._table_name,
                        "Key": {"pk": {"S": _run_lock_pk(run_id)}},
                    }
                },
            ]
        )
