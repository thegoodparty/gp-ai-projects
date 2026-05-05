import hashlib
import time
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from broker.auth import (
    AuthError,
    BrokerTokenAuth,
    get_broker_token,
    hash_service_token,
    verify_service_token,
)
from broker.dynamodb_client import ScopeTicket, ScopeTicketStore


class TestVerifyServiceToken:
    def test_correct_token_returns_true(self):
        token = "my-secret-token"
        expected_hash = hashlib.sha256(token.encode()).hexdigest()
        assert verify_service_token(token, expected_hash) is True

    def test_wrong_token_returns_false(self):
        token = "my-secret-token"
        wrong_hash = hashlib.sha256(b"wrong-token").hexdigest()
        assert verify_service_token(token, wrong_hash) is False


class TestHashServiceToken:
    def test_produces_consistent_output(self):
        token = "stable-token"
        h1 = hash_service_token(token)
        h2 = hash_service_token(token)
        assert h1 == h2
        assert h1 == hashlib.sha256(token.encode()).hexdigest()


class TestBrokerTokenAuth:
    def _make_ticket(self, exp_offset: int = 3600) -> ScopeTicket:
        now = int(time.time())
        return ScopeTicket(
            pk="valid-broker-token",
            run_id="run-001",
            organization_slug="org-42",
            experiment_id="voter_targeting",
            scope={"databricks": ["SELECT"]},
            params={"state": "CA"},
            exp=now + exp_offset,
            issued_at=now,
            issued_by="dispatch-lambda",
        )

    def test_valid_ticket_returns_ticket(self):
        ticket = self._make_ticket()
        mock_store = MagicMock(spec=ScopeTicketStore)
        mock_store.get_ticket.return_value = ticket

        auth = BrokerTokenAuth(store=mock_store)
        result = auth.verify("valid-broker-token")

        assert result is ticket
        mock_store.get_ticket.assert_called_once_with("valid-broker-token")

    def test_missing_ticket_raises_auth_error(self):
        mock_store = MagicMock(spec=ScopeTicketStore)
        mock_store.get_ticket.return_value = None

        auth = BrokerTokenAuth(store=mock_store)
        with pytest.raises(AuthError) as exc_info:
            auth.verify("missing-token")

        assert exc_info.value.reason_code == "scope_ticket_missing"

    def test_expired_ticket_raises_auth_error(self):
        mock_store = MagicMock(spec=ScopeTicketStore)
        mock_store.get_ticket.return_value = None

        auth = BrokerTokenAuth(store=mock_store)
        with pytest.raises(AuthError) as exc_info:
            auth.verify("expired-token")

        assert exc_info.value.reason_code == "scope_ticket_missing"


class TestGetBrokerToken:
    def _make_request(self, headers: dict | None = None) -> Request:
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        }
        return Request(scope)

    @pytest.mark.asyncio
    async def test_extracts_header(self):
        request = self._make_request({"x-broker-token": "abc-123"})
        result = await get_broker_token(request)
        assert result == "abc-123"

    @pytest.mark.asyncio
    async def test_missing_header_raises_401(self):
        request = self._make_request({})
        with pytest.raises(HTTPException) as exc_info:
            await get_broker_token(request)
        assert exc_info.value.status_code == 401
