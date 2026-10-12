"""``apply_codex_thread_collaboration_mode`` tests."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from omnigent.harnesses.codex_native import app_server


@pytest.mark.parametrize(
    ("effort", "expected_effort"),
    [("high", "high"), (None, None)],
)
async def test_collaboration_mode_switch_keeps_developer_instructions_null(
    monkeypatch: pytest.MonkeyPatch,
    effort: str | None,
    expected_effort: str | None,
) -> None:
    """The switch names the thread's model and effort but never a developer prompt:
    a value there would replace Codex's built-in Plan Mode instructions."""
    client = AsyncMock(spec=app_server.CodexAppServerClient)
    client.request.return_value = {"result": {}}
    client_calls: list[tuple[str, str]] = []

    def _client_for_transport(transport: str, *, client_name: str) -> AsyncMock:
        client_calls.append((transport, client_name))
        return client

    monkeypatch.setattr(app_server, "client_for_transport", _client_for_transport)

    await app_server.apply_codex_thread_collaboration_mode(
        "ws://127.0.0.1:1", "thread_fresh", "plan", model="gpt-5.4", effort=effort
    )

    assert client_calls == [("ws://127.0.0.1:1", "omnigent-codex-native-runner")]
    client.request.assert_awaited_once_with(
        "thread/settings/update",
        {
            "threadId": "thread_fresh",
            "collaborationMode": {
                "mode": "plan",
                "settings": {
                    "model": "gpt-5.4",
                    "reasoning_effort": expected_effort,
                    "developer_instructions": None,
                },
            },
        },
    )
    client.connect.assert_awaited_once()
    client.close.assert_awaited_once()


async def test_collaboration_mode_switch_closes_the_client_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = AsyncMock(spec=app_server.CodexAppServerClient)
    client.request.side_effect = RuntimeError("thread not loaded")
    monkeypatch.setattr(app_server, "client_for_transport", lambda *_a, **_k: client)

    with pytest.raises(RuntimeError, match="thread not loaded"):
        await app_server.apply_codex_thread_collaboration_mode(
            "ws://127.0.0.1:1", "thread_fresh", "plan", model="gpt-5.4"
        )

    client.close.assert_awaited_once()
