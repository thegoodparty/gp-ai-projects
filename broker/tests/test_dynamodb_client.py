import time
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws

from broker.dynamodb_client import (
    ScopeTicket,
    ScopeTicketStore,
    TicketAlreadyExistsError,
)


@pytest.fixture
def moto_ddb():
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-west-2")
        client.create_table(
            TableName="scope-tickets",
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield client


def _make_ticket(
    pk: str = "broker-token-abc",
    exp_offset: int = 3600,
    **overrides,
) -> ScopeTicket:
    now = int(time.time())
    defaults = {
        "pk": pk,
        "run_id": "run-001",
        "organization_slug": "org-42",
        "experiment_id": "voter_targeting",
        "scope": {"databricks": ["SELECT"], "tavily": True},
        "params": {"state": "CA", "district": "SD-15"},
        "exp": now + exp_offset,
        "issued_at": now,
        "issued_by": "dispatch-lambda",
    }
    defaults.update(overrides)
    return ScopeTicket(**defaults)


def _mock_dynamodb_client():
    return MagicMock()


class TestScopeTicketModel:
    def test_prior_artifact_versions_optional(self):
        ticket = _make_ticket()
        assert ticket.prior_artifact_versions is None

    def test_prior_artifact_versions_set(self):
        ticket = _make_ticket(prior_artifact_versions={"district_intel": "v2"})
        assert ticket.prior_artifact_versions == {"district_intel": "v2"}


class TestPutAndGetRoundTrip:
    def test_put_then_get_returns_ticket(self):
        mock_client = _mock_dynamodb_client()
        mock_client.transact_write_items.return_value = {}

        ticket = _make_ticket()
        store = ScopeTicketStore("scope-tickets", dynamodb_client=mock_client)
        store.put_ticket(ticket)

        mock_client.transact_write_items.assert_called_once()
        transact_items = mock_client.transact_write_items.call_args[1]["TransactItems"]
        assert len(transact_items) == 2
        assert transact_items[0]["Put"]["TableName"] == "scope-tickets"
        assert transact_items[0]["Put"]["Item"]["pk"]["S"] == ticket.pk
        assert transact_items[1]["Put"]["Item"]["pk"]["S"] == f"run:{ticket.run_id}"

        mock_client.get_item.return_value = {
            "Item": {
                "pk": {"S": ticket.pk},
                "run_id": {"S": ticket.run_id},
                "organization_slug": {"S": ticket.organization_slug},
                "experiment_id": {"S": ticket.experiment_id},
                "scope": {"S": ticket.scope.model_dump_json() if hasattr(ticket.scope, "model_dump_json") else __import__("json").dumps(ticket.scope)},
                "params": {"S": __import__("json").dumps(ticket.params)},
                "exp": {"N": str(ticket.exp)},
                "issued_at": {"N": str(ticket.issued_at)},
                "issued_by": {"S": ticket.issued_by},
            }
        }

        result = store.get_ticket(ticket.pk)
        assert result is not None
        assert result.pk == ticket.pk
        assert result.run_id == ticket.run_id
        assert result.organization_slug == ticket.organization_slug
        assert result.experiment_id == ticket.experiment_id
        assert result.scope == ticket.scope
        assert result.params == ticket.params


class TestGetMissing:
    def test_get_returns_none_for_missing_token(self):
        mock_client = _mock_dynamodb_client()
        mock_client.get_item.return_value = {}

        store = ScopeTicketStore("scope-tickets", dynamodb_client=mock_client)
        result = store.get_ticket("nonexistent-token")
        assert result is None


class TestGetExpired:
    def test_get_returns_none_for_expired_ticket(self):
        mock_client = _mock_dynamodb_client()
        expired_ticket = _make_ticket(exp_offset=-3600)

        mock_client.get_item.return_value = {
            "Item": {
                "pk": {"S": expired_ticket.pk},
                "run_id": {"S": expired_ticket.run_id},
                "organization_slug": {"S": expired_ticket.organization_slug},
                "experiment_id": {"S": expired_ticket.experiment_id},
                "scope": {"S": __import__("json").dumps(expired_ticket.scope)},
                "params": {"S": __import__("json").dumps(expired_ticket.params)},
                "exp": {"N": str(expired_ticket.exp)},
                "issued_at": {"N": str(expired_ticket.issued_at)},
                "issued_by": {"S": expired_ticket.issued_by},
            }
        }

        store = ScopeTicketStore("scope-tickets", dynamodb_client=mock_client)
        result = store.get_ticket(expired_ticket.pk)
        assert result is None


class TestPutDuplicate:
    def test_put_raises_ticket_already_exists_on_transaction_cancelled(self):
        mock_client = _mock_dynamodb_client()
        from botocore.exceptions import ClientError

        mock_client.transact_write_items.side_effect = ClientError(
            {
                "Error": {
                    "Code": "TransactionCanceledException",
                    "Message": "Transaction cancelled",
                },
                "CancellationReasons": [
                    {"Code": "None"},
                    {"Code": "ConditionalCheckFailed"},
                ],
            },
            "TransactWriteItems",
        )

        ticket = _make_ticket()
        store = ScopeTicketStore("scope-tickets", dynamodb_client=mock_client)

        with pytest.raises(TicketAlreadyExistsError):
            store.put_ticket(ticket)


class TestDelete:
    def test_delete_calls_delete_item(self):
        mock_client = _mock_dynamodb_client()
        mock_client.delete_item.return_value = {}

        store = ScopeTicketStore("scope-tickets", dynamodb_client=mock_client)
        store.delete_ticket("broker-token-abc")

        mock_client.delete_item.assert_called_once_with(
            TableName="scope-tickets",
            Key={"pk": {"S": "broker-token-abc"}},
        )


class TestRunIdIdempotency:
    def test_put_ticket_writes_run_lock_item(self, moto_ddb):
        store = ScopeTicketStore("scope-tickets", dynamodb_client=moto_ddb)
        ticket = _make_ticket(pk="token-xyz", run_id="run-ABC")

        store.put_ticket(ticket)

        run_lock = moto_ddb.get_item(
            TableName="scope-tickets",
            Key={"pk": {"S": "run:run-ABC"}},
        )
        assert "Item" in run_lock
        assert run_lock["Item"]["run_id"]["S"] == "run-ABC"
        assert run_lock["Item"]["broker_token"]["S"] == "token-xyz"
        assert int(run_lock["Item"]["exp"]["N"]) == ticket.exp

    def test_mint_rejects_duplicate_run_id(self, moto_ddb):
        store = ScopeTicketStore("scope-tickets", dynamodb_client=moto_ddb)
        first = _make_ticket(pk="token-1", run_id="run-DUPLICATE")
        second = _make_ticket(pk="token-2", run_id="run-DUPLICATE")

        store.put_ticket(first)

        with pytest.raises(TicketAlreadyExistsError):
            store.put_ticket(second)

        second_ticket = moto_ddb.get_item(
            TableName="scope-tickets",
            Key={"pk": {"S": "token-2"}},
        )
        assert "Item" not in second_ticket

    def test_mint_allows_new_run_id_after_expiry(self, moto_ddb):
        store = ScopeTicketStore("scope-tickets", dynamodb_client=moto_ddb)
        expired = _make_ticket(pk="token-old", run_id="run-REUSED", exp_offset=-60)
        fresh = _make_ticket(pk="token-new", run_id="run-REUSED")

        store.put_ticket(expired)
        store.put_ticket(fresh)

        run_lock = moto_ddb.get_item(
            TableName="scope-tickets",
            Key={"pk": {"S": "run:run-REUSED"}},
        )
        assert run_lock["Item"]["broker_token"]["S"] == "token-new"

    def test_get_ticket_rejects_run_lock_prefix(self, moto_ddb):
        store = ScopeTicketStore("scope-tickets", dynamodb_client=moto_ddb)
        ticket = _make_ticket(pk="token-real", run_id="run-GUARD")
        store.put_ticket(ticket)

        assert store.get_ticket("run:run-GUARD") is None

    def test_delete_ticket_and_run_lock_removes_both(self, moto_ddb):
        store = ScopeTicketStore("scope-tickets", dynamodb_client=moto_ddb)
        ticket = _make_ticket(pk="token-del", run_id="run-DEL")
        store.put_ticket(ticket)

        store.delete_ticket_and_run_lock("token-del", "run-DEL")

        ticket_row = moto_ddb.get_item(
            TableName="scope-tickets",
            Key={"pk": {"S": "token-del"}},
        )
        run_lock = moto_ddb.get_item(
            TableName="scope-tickets",
            Key={"pk": {"S": "run:run-DEL"}},
        )
        assert "Item" not in ticket_row
        assert "Item" not in run_lock

    def test_delete_then_remint_same_run_id_succeeds(self, moto_ddb):
        store = ScopeTicketStore("scope-tickets", dynamodb_client=moto_ddb)
        first = _make_ticket(pk="token-1", run_id="run-RETRY")
        store.put_ticket(first)
        store.delete_ticket_and_run_lock("token-1", "run-RETRY")

        retry = _make_ticket(pk="token-2", run_id="run-RETRY")
        store.put_ticket(retry)

        run_lock = moto_ddb.get_item(
            TableName="scope-tickets",
            Key={"pk": {"S": "run:run-RETRY"}},
        )
        assert run_lock["Item"]["broker_token"]["S"] == "token-2"
