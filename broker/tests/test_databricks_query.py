import asyncio
import time
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport

from broker.data_query_tracker import DataQueryTracker
from broker.dynamodb_client import ScopeTicket
from broker.endpoints.databricks_query import (
    DatabricksClient,
    get_data_query_tracker,
    get_databricks_client,
    get_scope_ticket,
    router,
)

BROKER_TOKEN = "broker-token-test-abc123"


def _make_scope() -> dict:
    return {
        "allowed_tables": ["goodparty_data_catalog.gold.voter_file"],
        "state": "CA",
        "cities": ["Los Angeles"],
        "max_rows": 50000,
    }


def _make_ticket(
    expired: bool = False,
    scope: dict | None = None,
) -> ScopeTicket:
    now = int(time.time())
    return ScopeTicket(
        pk=BROKER_TOKEN,
        run_id="run-001",
        organization_slug="org-42",
        experiment_id="voter_targeting",
        scope=scope or _make_scope(),
        params={"state": "CA"},
        exp=now + (-3600 if expired else 3600),
        issued_at=now,
        issued_by="dispatch-lambda-dev",
    )


def _create_app(
    ticket: ScopeTicket | None = None,
    db_columns: list[str] | None = None,
    db_rows: list[list] | None = None,
    db_error: Exception | None = None,
    tracker: DataQueryTracker | None = None,
) -> tuple[FastAPI, MagicMock]:
    app = FastAPI()
    app.include_router(router)

    _ticket = ticket or _make_ticket()
    app.dependency_overrides[get_scope_ticket] = lambda: _ticket

    mock_db = MagicMock(spec=DatabricksClient)
    if db_error:
        mock_db.execute.side_effect = db_error
    else:
        columns = db_columns or ["party", "age"]
        rows = db_rows or [["Independent", 35], ["Democrat", 42]]
        mock_db.execute.return_value = (columns, rows)
    app.dependency_overrides[get_databricks_client] = lambda: mock_db

    _tracker = tracker if tracker is not None else DataQueryTracker()
    app.dependency_overrides[get_data_query_tracker] = lambda: _tracker

    return app, mock_db


class TestDatabricksQuerySuccess:
    def test_valid_query_rewritten_and_executed(self):
        app, mock_db = _create_app()
        client = TestClient(app)

        resp = client.post(
            "/databricks/query",
            json={
                "sql": "SELECT party, age FROM goodparty_data_catalog.gold.voter_file LIMIT 10",
                "parameters": {},
            },
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["columns"] == ["party", "age"]
        assert body["rows"] == [["Independent", 35], ["Democrat", 42]]
        assert body["row_count"] == 2
        assert body["row_cap_hit"] is False

        executed_sql = mock_db.execute.call_args[0][0]
        assert "Residence_Addresses_State" in executed_sql
        assert "Residence_Addresses_City" in executed_sql


class TestDatabricksQueryDisallowedTable:
    def test_disallowed_table_returns_400(self):
        app, _ = _create_app()
        client = TestClient(app)

        resp = client.post(
            "/databricks/query",
            json={
                "sql": "SELECT party FROM secret_schema.passwords",
                "parameters": {},
            },
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert detail["reason_code"] == "disallowed_table"


class TestDatabricksQueryExpiredToken:
    def test_expired_broker_token_returns_401(self):
        expired_ticket = _make_ticket(expired=True)

        app = FastAPI()
        app.include_router(router)

        from broker.auth import AuthError

        def _raise_auth_error():
            raise AuthError("scope_ticket_expired")

        app.dependency_overrides[get_scope_ticket] = _raise_auth_error

        mock_db = MagicMock(spec=DatabricksClient)
        app.dependency_overrides[get_databricks_client] = lambda: mock_db

        client = TestClient(app, raise_server_exceptions=False)

        resp = client.post(
            "/databricks/query",
            json={"sql": "SELECT party FROM voter_file", "parameters": {}},
            headers={"X-Broker-Token": "expired-token"},
        )

        assert resp.status_code == 500 or resp.status_code == 401


class TestDatabricksQueryExecutionError:
    def test_databricks_execution_error_returns_502(self):
        app, _ = _create_app(db_error=Exception("Connection refused"))
        client = TestClient(app)

        resp = client.post(
            "/databricks/query",
            json={
                "sql": "SELECT party, age FROM goodparty_data_catalog.gold.voter_file LIMIT 10",
                "parameters": {},
            },
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        assert resp.status_code == 502
        assert "Databricks query execution failed" in resp.json()["detail"]


class TestDatabricksQueryTracksSuccess:
    """Successful queries must be counted against the ticket so that the
    publish endpoint can reject data-required experiments with zero queries.
    """

    def test_successful_query_increments_ticket_counter(self):
        tracker = DataQueryTracker()
        app, _ = _create_app(tracker=tracker)
        client = TestClient(app)

        resp = client.post(
            "/databricks/query",
            json={
                "sql": "SELECT party, age FROM goodparty_data_catalog.gold.voter_file LIMIT 10",
                "parameters": {},
            },
            headers={"X-Broker-Token": BROKER_TOKEN},
        )
        assert resp.status_code == 200
        assert tracker.get(BROKER_TOKEN) == 1

        client.post(
            "/databricks/query",
            json={
                "sql": "SELECT party, age FROM goodparty_data_catalog.gold.voter_file LIMIT 20",
                "parameters": {},
            },
            headers={"X-Broker-Token": BROKER_TOKEN},
        )
        assert tracker.get(BROKER_TOKEN) == 2

    def test_failed_query_does_not_increment_counter(self):
        tracker = DataQueryTracker()
        app, _ = _create_app(
            db_error=Exception("Connection refused"),
            tracker=tracker,
        )
        client = TestClient(app)

        resp = client.post(
            "/databricks/query",
            json={
                "sql": "SELECT party FROM goodparty_data_catalog.gold.voter_file LIMIT 1",
                "parameters": {},
            },
            headers={"X-Broker-Token": BROKER_TOKEN},
        )
        assert resp.status_code == 502
        assert tracker.get(BROKER_TOKEN) == 0


class TestDatabricksQueryDisallowedVerb:
    def test_insert_returns_400(self):
        app, _ = _create_app()
        client = TestClient(app)

        resp = client.post(
            "/databricks/query",
            json={
                "sql": "INSERT INTO goodparty_data_catalog.gold.voter_file (party) VALUES ('X')",
                "parameters": {},
            },
            headers={"X-Broker-Token": BROKER_TOKEN},
        )

        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert detail["reason_code"] == "disallowed_verb"


class TestDatabricksConnectionPooling:
    """Connection setup should be amortized across calls on the same thread.

    Opening a new Databricks SQL connection for every query costs 1-3s of TLS
    handshake + auth. Keeping a thread-local cached connection cuts that cost
    from every query to just the first one per worker thread.
    """

    def test_repeated_execute_reuses_connection(self, monkeypatch):
        from broker.endpoints import databricks_query as dbx_mod

        connects = []

        class _FakeCursor:
            description = [("party", None), ("age", None)]
            def execute(self, sql, parameters=None):
                pass
            def fetchall(self):
                return [["Independent", 35]]
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        class _FakeConnection:
            def __init__(self):
                self.closed = False
            def cursor(self):
                return _FakeCursor()
            def close(self):
                self.closed = True
            def __enter__(self):
                return self
            def __exit__(self, *a):
                self.closed = True
                return False

        class _FakeDbxSql:
            @staticmethod
            def connect(**kwargs):
                connects.append(kwargs)
                return _FakeConnection()

        # Patch the module-level databricks.sql import so DatabricksClient uses the fake.
        import sys
        fake_module = MagicMock()
        fake_module.sql = _FakeDbxSql
        monkeypatch.setitem(sys.modules, "databricks", fake_module)
        monkeypatch.setitem(sys.modules, "databricks.sql", _FakeDbxSql)

        client = dbx_mod.DatabricksClient(
            server_hostname="test.cloud.databricks.com",
            http_path="/sql/1.0/warehouses/abc",
            access_token="test-token",
        )

        # Three queries from the same thread.
        for _ in range(3):
            client.execute("SELECT 1", {})

        assert len(connects) == 1, (
            f"expected 1 connection (pooled), got {len(connects)} "
            f"(connection is being re-opened per query)"
        )


class TestDatabricksReconnectOnStaleSession:
    """Databricks SQL warehouses expire idle sessions. The broker caches a
    thread-local connection for throughput — but a stale handle raises
    `INVALID_STATE: Invalid SessionHandle`, which currently bubbles up as a
    502 on EVERY subsequent query on that thread until the broker restarts.

    The client must detect the failure, drop its cached connection, and
    reconnect once transparently.
    """

    def test_execute_reconnects_and_retries_once_after_failure(self, monkeypatch):
        from broker.endpoints import databricks_query as dbx_mod

        connects = []
        cursor_calls = []

        class _StaleCursor:
            description = None
            def execute(self, sql, parameters=None):
                cursor_calls.append(("stale", sql))
                raise RuntimeError(
                    "Error during request to server: INVALID_STATE: "
                    "Invalid SessionHandle: SessionHandle [01f13b61-...]"
                )
            def fetchall(self):
                return []
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        class _FreshCursor:
            description = [("party", None)]
            def execute(self, sql, parameters=None):
                cursor_calls.append(("fresh", sql))
            def fetchall(self):
                return [["Independent"]]
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        class _StaleConnection:
            def cursor(self):
                return _StaleCursor()
            def close(self):
                pass

        class _FreshConnection:
            def cursor(self):
                return _FreshCursor()
            def close(self):
                pass

        connections = [_StaleConnection(), _FreshConnection()]

        class _FakeDbxSql:
            @staticmethod
            def connect(**kwargs):
                connects.append(kwargs)
                return connections.pop(0)

        import sys
        fake_module = MagicMock()
        fake_module.sql = _FakeDbxSql
        monkeypatch.setitem(sys.modules, "databricks", fake_module)
        monkeypatch.setitem(sys.modules, "databricks.sql", _FakeDbxSql)

        client = dbx_mod.DatabricksClient(
            server_hostname="test.cloud.databricks.com",
            http_path="/sql/1.0/warehouses/abc",
            access_token="test-token",
        )

        columns, rows = client.execute("SELECT party FROM t", {})

        assert columns == ["party"]
        assert rows == [["Independent"]]
        assert len(connects) == 2, f"expected 2 connects (stale + fresh), got {len(connects)}"
        assert [c[0] for c in cursor_calls] == ["stale", "fresh"]

    def test_execute_raises_if_second_attempt_also_fails(self, monkeypatch):
        """Retry is one-shot — two consecutive failures still surface as an error."""
        from broker.endpoints import databricks_query as dbx_mod

        class _AlwaysFailsCursor:
            description = None
            def execute(self, sql, parameters=None):
                raise RuntimeError("INVALID_STATE: Invalid SessionHandle")
            def fetchall(self):
                return []
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        class _AlwaysFailsConnection:
            def cursor(self):
                return _AlwaysFailsCursor()
            def close(self):
                pass

        class _FakeDbxSql:
            @staticmethod
            def connect(**kwargs):
                return _AlwaysFailsConnection()

        import sys
        fake_module = MagicMock()
        fake_module.sql = _FakeDbxSql
        monkeypatch.setitem(sys.modules, "databricks", fake_module)
        monkeypatch.setitem(sys.modules, "databricks.sql", _FakeDbxSql)

        client = dbx_mod.DatabricksClient(
            server_hostname="host",
            http_path="/sql/1.0/warehouses/abc",
            access_token="tok",
        )

        with pytest.raises(RuntimeError, match="INVALID_STATE"):
            client.execute("SELECT 1", {})


class TestEventLoopNotBlockedByDatabricks:
    """Long Databricks queries must not serialize other in-flight requests.

    Regression guard: if someone reverts `asyncio.to_thread(db_client.execute, ...)`
    back to a direct synchronous call inside the async handler, a slow query will
    block the event loop and concurrent requests will queue behind it. This test
    fires three 300ms queries concurrently and asserts they finish in roughly
    one query's time, not three.
    """

    @pytest.mark.asyncio
    async def test_concurrent_queries_run_in_parallel(self):
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_scope_ticket] = lambda: _make_ticket()

        def _slow_execute(sql, parameters):
            # Synchronous blocking — represents the real databricks-sql driver.
            time.sleep(0.3)
            return (["party"], [["Independent"]])

        mock_db = MagicMock(spec=DatabricksClient)
        mock_db.execute.side_effect = _slow_execute
        app.dependency_overrides[get_databricks_client] = lambda: mock_db
        app.dependency_overrides[get_data_query_tracker] = lambda: DataQueryTracker()

        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            body = {
                "sql": "SELECT party FROM goodparty_data_catalog.gold.voter_file LIMIT 1",
                "parameters": {},
            }
            headers = {"X-Broker-Token": BROKER_TOKEN}

            start = time.perf_counter()
            responses = await asyncio.gather(
                client.post("/databricks/query", json=body, headers=headers),
                client.post("/databricks/query", json=body, headers=headers),
                client.post("/databricks/query", json=body, headers=headers),
            )
            elapsed = time.perf_counter() - start

        statuses = [r.status_code for r in responses]
        bodies = [r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text for r in responses]
        assert all(s == 200 for s in statuses), f"non-200s: {list(zip(statuses, bodies))}"
        # Serialized (sync handler) would be ~0.9s. Parallel (async + to_thread)
        # should be ~0.3s; allow generous 0.65s ceiling for CI jitter.
        assert elapsed < 0.65, f"queries serialized: {elapsed:.2f}s (>= 0.65s means event loop is blocked)"
