import httpx


class ScopeViolation(Exception):
    def __init__(self, reason_code: str, detail: str):
        self.reason_code = reason_code
        self.detail = detail
        super().__init__(f"{reason_code}: {detail}")


class UpstreamError(Exception):
    pass


class Cursor:
    def __init__(self, client: httpx.Client):
        self._client = client
        self._rows: list[tuple] | None = None
        self._columns: list[str] | None = None
        self.description: list[tuple] | None = None

    def execute(self, sql: str, parameters: dict | None = None):
        response = self._client.post("/databricks/query", json={
            "sql": sql,
            "parameters": parameters or {},
        })
        if response.status_code == 400:
            data = response.json()
            payload = data.get("detail") if isinstance(data.get("detail"), dict) else data
            raise ScopeViolation(payload.get("reason_code", "unknown"), payload.get("detail", ""))
        if response.status_code >= 500:
            raise UpstreamError(response.text)
        response.raise_for_status()
        data = response.json()
        self._columns = data.get("columns", [])
        rows = data.get("rows", [])
        if rows and isinstance(rows[0], dict):
            self._rows = [tuple(row.get(col) for col in self._columns) for row in rows]
        else:
            self._rows = [tuple(row) for row in rows]
        self.description = [(col, None, None, None, None, None, None) for col in self._columns]

    def fetchall(self) -> list[tuple]:
        return self._rows or []

    def fetchone(self) -> tuple | None:
        if not self._rows:
            return None
        return self._rows.pop(0)

    def close(self):
        pass


class Connection:
    def __init__(self, client: httpx.Client):
        self._client = client

    def cursor(self) -> Cursor:
        return Cursor(self._client)

    def close(self):
        pass


def connect(**kwargs) -> Connection:
    from .config import get_config
    return Connection(get_config().client)
