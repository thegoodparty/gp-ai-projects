import boto3
import pytest
from moto import mock_aws

from broker.data_query_tracker import DataQueryTracker

TABLE = "scope-tickets"


@pytest.fixture
def moto_ddb():
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-west-2")
        client.create_table(
            TableName=TABLE,
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield client


class RecordingClient:
    """Delegates to a real (moto) client but records get_item kwargs so the
    test can assert the publish-time read is strongly consistent."""

    def __init__(self, inner):
        self._inner = inner
        self.get_item_calls = []

    def update_item(self, **kwargs):
        return self._inner.update_item(**kwargs)

    def get_item(self, **kwargs):
        self.get_item_calls.append(kwargs)
        return self._inner.get_item(**kwargs)


def test_increment_on_one_instance_is_visible_from_another(moto_ddb):
    # Two trackers over the same table = two broker instances behind the ALB.
    broker_a = DataQueryTracker(table_name=TABLE, dynamodb_client=moto_ddb)
    broker_b = DataQueryTracker(table_name=TABLE, dynamodb_client=moto_ddb)

    broker_a.increment("ticket-1")

    # The query hit broker A; the publish lands on broker B. It MUST see it.
    assert broker_b.get("ticket-1") == 1


def test_increments_accumulate_across_instances(moto_ddb):
    a = DataQueryTracker(table_name=TABLE, dynamodb_client=moto_ddb)
    b = DataQueryTracker(table_name=TABLE, dynamodb_client=moto_ddb)

    a.increment("ticket-2")
    b.increment("ticket-2")
    a.increment("ticket-2")

    assert b.get("ticket-2") == 3


def test_get_unknown_ticket_returns_zero(moto_ddb):
    t = DataQueryTracker(table_name=TABLE, dynamodb_client=moto_ddb)
    assert t.get("never-seen") == 0


def test_counts_are_isolated_per_ticket(moto_ddb):
    t = DataQueryTracker(table_name=TABLE, dynamodb_client=moto_ddb)
    t.increment("ticket-a")
    t.increment("ticket-a")
    t.increment("ticket-b")

    assert t.get("ticket-a") == 2
    assert t.get("ticket-b") == 1


def test_clear_resets_count(moto_ddb):
    t = DataQueryTracker(table_name=TABLE, dynamodb_client=moto_ddb)
    t.increment("ticket-3")
    t.clear("ticket-3")
    assert t.get("ticket-3") == 0


def test_get_uses_strongly_consistent_read(moto_ddb):
    recorder = RecordingClient(moto_ddb)
    t = DataQueryTracker(table_name=TABLE, dynamodb_client=recorder)
    t.increment("ticket-4")
    t.get("ticket-4")

    assert recorder.get_item_calls, "expected a get_item call"
    assert all(c.get("ConsistentRead") is True for c in recorder.get_item_calls)
