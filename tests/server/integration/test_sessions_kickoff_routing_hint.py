"""The create kickoff hands the forwarder the session's resolved spec.

The first turn of a session created with ``initial_items`` routes like any
later turn, so the kickoff must thread the agent spec into the forwarder.
Without it the forwarder cannot tell that an off-gateway pane reaches its
model direct from the vendor, and would pin a gateway-hosted pick the pane
cannot run -- the same defect the ``POST /events`` path already guards.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio


async def test_create_kickoff_threads_agent_spec_to_the_forwarder(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A kickoff item is dispatched with the agent's resolved spec, not ``None``."""
    import omnigent.server.routes.sessions as sessions_module

    captured: dict[str, Any] = {}

    fake_runner = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _req: httpx.Response(202, json={"queued": True})),
        base_url="http://runner",
    )

    async def _get_runner_client(*_: Any, **__: Any) -> httpx.AsyncClient:
        return fake_runner

    async def _ensure_runner_relay_ready(*_: Any, **__: Any) -> None:
        return None

    async def _capture_dispatch(*_: Any, **kwargs: Any) -> str:
        captured["seen"] = True
        captured["agent_spec"] = kwargs.get("agent_spec")
        return "item_kickoff"

    monkeypatch.setattr(sessions_module, "_get_runner_client", _get_runner_client)
    monkeypatch.setattr(sessions_module, "_ensure_runner_relay_ready", _ensure_runner_relay_ready)
    monkeypatch.setattr(sessions_module, "_dispatch_session_event_to_runner", _capture_dispatch)

    try:
        agent = await create_test_agent(client)
        resp = await client.post(
            "/v1/sessions",
            json={
                "agent_id": agent["id"],
                "initial_items": [
                    {
                        "type": "message",
                        "data": {
                            "role": "user",
                            "content": [{"type": "input_text", "text": "hello"}],
                        },
                    }
                ],
            },
        )
        assert resp.status_code == 201, resp.text
    finally:
        await fake_runner.aclose()

    assert captured.get("seen"), "the kickoff item was never dispatched"
    assert captured["agent_spec"] is not None
