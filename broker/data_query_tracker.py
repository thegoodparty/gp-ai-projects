import threading


class DataQueryTracker:
    """Per-ticket counter of successful Databricks queries.

    Process-local. Used by the artifact publish endpoint to gate experiments
    that require real voter data: if the count is zero we assume the agent
    fabricated its output and reject the publish.

    Single broker task today means a broker restart during a run clears the
    counter and the publish would be rejected — strictly safer than the
    previous behavior of accepting any schema-valid artifact.
    """

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}
        self._lock = threading.Lock()

    def increment(self, ticket_pk: str) -> None:
        with self._lock:
            self._counts[ticket_pk] = self._counts.get(ticket_pk, 0) + 1

    def get(self, ticket_pk: str) -> int:
        with self._lock:
            return self._counts.get(ticket_pk, 0)

    def clear(self, ticket_pk: str) -> None:
        with self._lock:
            self._counts.pop(ticket_pk, None)
