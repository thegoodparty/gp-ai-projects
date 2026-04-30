import json
import httpx
import pytest

from pmf_engine.runner.pmf_runtime.config import PMFRuntimeConfig, init_config
from pmf_engine.runner.pmf_runtime.databricks import (
    Cursor,
    Connection,
    ScopeViolation,
    UpstreamError,
    connect,
)


def _make_client(handler):
    transport = httpx.MockTransport(handler)
    return httpx.Client(transport=transport, base_url="http://broker")


def _query_response(columns, rows, status=200):
    def handler(request):
        return httpx.Response(status, json={"columns": columns, "rows": rows})
    return handler


class TestCursor:
    def test_execute_returns_rows_as_tuples(self):
        columns = ["id", "name"]
        rows = [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}]
        client = _make_client(_query_response(columns, rows))

        cursor = Cursor(client)
        cursor.execute("SELECT id, name FROM users")
        result = cursor.fetchall()

        assert result == [(1, "Alice"), (2, "Bob")]

    def test_execute_sets_description(self):
        columns = ["id", "name"]
        rows = [{"id": 1, "name": "Alice"}]
        client = _make_client(_query_response(columns, rows))

        cursor = Cursor(client)
        cursor.execute("SELECT id, name FROM users")

        assert cursor.description == [
            ("id", None, None, None, None, None, None),
            ("name", None, None, None, None, None, None),
        ]

    def test_execute_with_parameters(self):
        captured = {}

        def handler(request):
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={"columns": ["x"], "rows": [{"x": 1}]})

        client = _make_client(handler)
        cursor = Cursor(client)
        cursor.execute("SELECT * FROM t WHERE id = :id", {"id": 42})

        assert captured["body"]["parameters"] == {"id": 42}

    def test_execute_400_raises_scope_violation(self):
        def handler(request):
            return httpx.Response(400, json={
                "reason_code": "WRITE_DENIED",
                "detail": "INSERT not allowed",
            })

        client = _make_client(handler)
        cursor = Cursor(client)

        with pytest.raises(ScopeViolation) as exc_info:
            cursor.execute("INSERT INTO t VALUES (1)")
        assert exc_info.value.reason_code == "WRITE_DENIED"
        assert "INSERT not allowed" in exc_info.value.detail

    def test_execute_400_unwraps_fastapi_nested_detail(self):
        def handler(request):
            return httpx.Response(400, json={
                "detail": {
                    "reason_code": "scope_forbidden_function",
                    "detail": "explode() not allowed",
                },
            })

        client = _make_client(handler)
        cursor = Cursor(client)

        with pytest.raises(ScopeViolation) as exc_info:
            cursor.execute("SELECT explode(arr) FROM t")
        assert exc_info.value.reason_code == "scope_forbidden_function"
        assert exc_info.value.detail == "explode() not allowed"

    def test_execute_500_raises_upstream_error(self):
        def handler(request):
            return httpx.Response(500, text="Internal Server Error")

        client = _make_client(handler)
        cursor = Cursor(client)

        with pytest.raises(UpstreamError):
            cursor.execute("SELECT 1")

    def test_fetchall_empty(self):
        client = _make_client(_query_response([], []))
        cursor = Cursor(client)
        cursor.execute("SELECT 1")
        assert cursor.fetchall() == []

    def test_fetchone_returns_rows_one_at_a_time(self):
        columns = ["v"]
        rows = [{"v": "a"}, {"v": "b"}, {"v": "c"}]
        client = _make_client(_query_response(columns, rows))

        cursor = Cursor(client)
        cursor.execute("SELECT v FROM t")

        assert cursor.fetchone() == ("a",)
        assert cursor.fetchone() == ("b",)
        assert cursor.fetchone() == ("c",)
        assert cursor.fetchone() is None

    def test_fetchone_no_execute(self):
        client = _make_client(lambda r: httpx.Response(200, json={}))
        cursor = Cursor(client)
        assert cursor.fetchone() is None

    def test_close_is_noop(self):
        client = _make_client(lambda r: httpx.Response(200, json={}))
        cursor = Cursor(client)
        cursor.close()


class TestConnection:
    def test_cursor_returns_cursor(self):
        client = _make_client(lambda r: httpx.Response(200, json={}))
        conn = Connection(client)
        cursor = conn.cursor()
        assert isinstance(cursor, Cursor)

    def test_close_is_noop(self):
        client = _make_client(lambda r: httpx.Response(200, json={}))
        conn = Connection(client)
        conn.close()


class TestConnect:
    def setup_method(self):
        import pmf_engine.runner.pmf_runtime.config as config_mod
        config_mod._config = None

    def test_connect_uses_config_client(self):
        transport = httpx.MockTransport(
            lambda r: httpx.Response(200, json={"columns": ["x"], "rows": [{"x": 1}]})
        )
        client = httpx.Client(transport=transport, base_url="http://broker")
        cfg = init_config("http://broker", "tok")
        cfg._client = client

        conn = connect()
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        assert cursor.fetchall() == [(1,)]
