import asyncio
import logging
import threading

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from broker.auth import BrokerTokenAuth, get_broker_token
from broker.data_query_tracker import DataQueryTracker
from broker.dynamodb_client import ScopeTicket
from broker.sql_rewriter import ScopeViolation, rewrite_query, validate_parameters

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/databricks", tags=["databricks"])

ROW_CAP = 50000


class QueryRequest(BaseModel):
    sql: str
    parameters: dict[str, str | int | float | bool | None] = {}


class QueryResponse(BaseModel):
    columns: list[str]
    rows: list[list]
    row_count: int
    row_cap_hit: bool


class DatabricksClient:
    def __init__(self, server_hostname: str, http_path: str, access_token: str):
        self._server_hostname = server_hostname
        self._http_path = http_path
        self._access_token = access_token
        # Databricks SQL Connection is not thread-safe, but asyncio.to_thread
        # runs each call on a pool thread — so we keep one connection per thread.
        # First query on a given thread pays the ~1-3s TLS/auth cost; subsequent
        # queries reuse the existing connection.
        self._local = threading.local()

    def _get_connection(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            from databricks import sql as dbx_sql
            conn = dbx_sql.connect(
                server_hostname=self._server_hostname,
                http_path=self._http_path,
                access_token=self._access_token,
            )
            self._local.conn = conn
        return conn

    def _reset_connection(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self._local.conn = None

    def _execute_once(self, sql: str, parameters: dict | None) -> tuple[list[str], list[list]]:
        connection = self._get_connection()
        with connection.cursor() as cursor:
            cursor.execute(sql, parameters=parameters or {})
            columns = [desc[0] for desc in cursor.description] if cursor.description else []
            rows = [list(row) for row in cursor.fetchall()]
            return columns, rows

    def execute(self, sql: str, parameters: dict | None = None) -> tuple[list[str], list[list]]:
        try:
            return self._execute_once(sql, parameters)
        except Exception:
            # Databricks expires idle sessions; the cached thread-local connection's
            # SessionHandle goes invalid and every subsequent query 502s until the
            # broker restarts. Drop the connection and retry once so the failure
            # self-heals. If the retry also fails, surface the error.
            logger.warning("databricks execute failed — reconnecting and retrying once", exc_info=True)
            self._reset_connection()
            return self._execute_once(sql, parameters)


def get_scope_ticket() -> ScopeTicket:  # pragma: no cover
    raise NotImplementedError("Must be overridden via dependency_overrides")


def get_databricks_client() -> DatabricksClient:  # pragma: no cover
    raise NotImplementedError("Must be overridden via dependency_overrides")


def get_data_query_tracker() -> DataQueryTracker:  # pragma: no cover
    raise NotImplementedError("Must be overridden via dependency_overrides")


@router.post("/query", response_model=QueryResponse)
async def databricks_query(
    req: QueryRequest,
    ticket: ScopeTicket = Depends(get_scope_ticket),
    db_client: DatabricksClient = Depends(get_databricks_client),
    tracker: DataQueryTracker = Depends(get_data_query_tracker),
):
    scope = ticket.scope

    try:
        result = rewrite_query(req.sql, scope, req.parameters)
    except ScopeViolation as exc:
        raise HTTPException(
            status_code=400,
            detail={"reason_code": exc.reason_code, "detail": exc.detail},
        )

    try:
        validate_parameters(result.sql, req.parameters or {})
    except ScopeViolation as exc:
        raise HTTPException(
            status_code=400,
            detail={"reason_code": exc.reason_code, "detail": exc.detail},
        )

    try:
        # The databricks-sql connector is sync-only. Run it on a worker thread so
        # the event loop stays free to service other requests during long queries.
        columns, rows = await asyncio.to_thread(
            db_client.execute, result.sql, req.parameters or None
        )
    except Exception:
        logger.error(
            "databricks query failed run_id=%s sql=%s",
            ticket.run_id, result.sql[:200],
            exc_info=True,
        )
        raise HTTPException(status_code=502, detail="Databricks query execution failed")

    row_cap_hit = len(rows) >= ROW_CAP

    tracker.increment(ticket.pk)

    return QueryResponse(
        columns=columns,
        rows=rows,
        row_count=len(rows),
        row_cap_hit=row_cap_hit,
    )
