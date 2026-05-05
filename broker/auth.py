import hashlib
import hmac

from fastapi import HTTPException
from starlette.requests import Request

from broker.dynamodb_client import ScopeTicket, ScopeTicketStore


class AuthError(Exception):
    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


def verify_service_token(token: str, expected_hash: str) -> bool:
    return hmac.compare_digest(
        hashlib.sha256(token.encode()).hexdigest(),
        expected_hash,
    )


def hash_service_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class BrokerTokenAuth:
    def __init__(self, store: ScopeTicketStore):
        self._store = store

    def verify(self, broker_token: str) -> ScopeTicket:
        ticket = self._store.get_ticket(broker_token)
        if ticket is None:
            raise AuthError("scope_ticket_missing")
        return ticket


async def get_broker_token(request: Request) -> str:
    token = request.headers.get("x-broker-token")
    if not token:
        raise HTTPException(status_code=401, detail="Missing X-Broker-Token header")
    return token


async def get_service_token(request: Request) -> str:
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    return auth_header[7:]
