"""A database statement cancelled at its time limit is reported as a timeout, not a fault."""

from __future__ import annotations

import logging

import httpx
import pytest
from sqlalchemy.exc import OperationalError

from omnigent.errors import ErrorCategory
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


class _QueryCanceled(Exception):
    """Stand-in for psycopg's ``QueryCanceled``: SQLSTATE 57014 on ``sqlstate``."""

    sqlstate = "57014"


class _TooManyConnections(Exception):
    sqlstate = "53300"


def _raise_from_list_conversations(monkeypatch: pytest.MonkeyPatch, orig: Exception) -> None:
    """Make the session listing fail the way the driver reports *orig*."""
    error = OperationalError("SELECT conversations.id FROM conversations ...", {}, orig)

    def _list(*args: object, **kwargs: object) -> None:
        raise error

    monkeypatch.setattr(SqlAlchemyConversationStore, "list_conversations", _list)


async def test_cancelled_search_statement_answers_query_timeout(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The content search's ``statement_timeout`` firing is a 504 ``query_timeout``."""
    _raise_from_list_conversations(
        monkeypatch, _QueryCanceled("canceling statement due to statement timeout")
    )
    with caplog.at_level(logging.WARNING, logger="omnigent.server.app"):
        response = await client.get(
            "/v1/sessions", params={"search_query": "zzqqxwv", "limit": 20}
        )

    assert response.status_code == 504, response.text
    error = response.json()["error"]
    assert error["code"] == "query_timeout"
    assert "more specific search term" in error["message"]
    (record,) = [
        r
        for r in caplog.records
        if r.name == "omnigent.server.app" and "cancelled at its time limit" in r.getMessage()
    ]
    assert record.levelno == logging.WARNING
    assert "statement timeout" in record.getMessage()
    assert record.attributes["code"] == "query_timeout"
    assert record.attributes["http_status"] == "504"
    assert record.attributes["error_category"] == ErrorCategory.SERVER.value


async def test_other_database_errors_stay_internal_errors(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Only a cancelled statement is a timeout; a real database failure is still a 500."""
    _raise_from_list_conversations(monkeypatch, _TooManyConnections("too many clients already"))
    with caplog.at_level(logging.ERROR, logger="omnigent.server.app"):
        response = await client.get("/v1/sessions", params={"search_query": "zzqqxwv"})

    assert response.status_code == 500, response.text
    assert response.json()["error"]["code"] == "internal_error"
    assert any(r.getMessage().startswith("Database error:") for r in caplog.records)
