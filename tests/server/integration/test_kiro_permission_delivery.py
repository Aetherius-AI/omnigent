"""Kiro delivery state through the real hook, resolve, snapshot and event routes."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from omnigent.runtime import pending_elicitations
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.server.helpers import create_session_for_agent, create_test_agent

pytestmark = pytest.mark.asyncio


async def _wait_for_state(elicitation_id: str, state: str) -> None:
    async with asyncio.timeout(5):
        while True:
            entry = pending_elicitations.lookup(elicitation_id)
            if entry and entry[1]["params"]["native_delivery"]["state"] == state:
                return
            await asyncio.sleep(0.01)


async def test_kiro_delivery_stays_pending_and_retries_only_the_current_attempt(
    client: httpx.AsyncClient,
) -> None:
    agent = await create_test_agent(client, "kiro-delivery")
    session_id = await create_session_for_agent(client, agent["id"])
    eid = f"elicit_kiro_{session_id}"
    hook_url = f"/v1/sessions/{session_id}/hooks/native-permission-request"
    resolve_url = f"/v1/sessions/{session_id}/elicitations/{eid}/resolve"
    payload = {
        "elicitation_id": eid,
        "agent": "Kiro",
        "policy_name": "kiro_native_permission",
        "message": "Kiro wants approval for Running: pwd",
        "content_preview": "Running: pwd",
        "native_delivery": {"state": "ready", "attempt": "first"},
    }
    first = asyncio.create_task(client.post(hook_url, json=payload))
    await _wait_for_state(eid, "ready")
    response = await client.post(resolve_url, json={"action": "accept"})
    assert response.status_code == 202
    assert (await first).json()["action"] == "accept"
    await _wait_for_state(eid, "sending")
    snapshot = (await client.get(f"/v1/sessions/{session_id}")).json()
    assert len(snapshot["pending_elicitations"]) == 1
    detail = (await client.get(f"/v1/sessions/{session_id}/elicitations/{eid}")).json()
    assert detail["native_delivery"]["state"] == "sending"

    payload["native_delivery"] = {"state": "unconfirmed", "attempt": "second", "action": "accept"}
    retry = asyncio.create_task(client.post(hook_url, json=payload))
    try:
        await _wait_for_state(eid, "unconfirmed")
        stale = await client.post(
            resolve_url,
            json={
                "action": "accept",
                "_meta": {"native_delivery_attempt": "first"},
            },
        )
        assert stale.status_code == 400
        assert not retry.done()
        changed_decision = await client.post(
            resolve_url,
            json={"action": "decline", "_meta": {"native_delivery_attempt": "second"}},
        )
        assert changed_decision.status_code == 400
        assert not retry.done()
        verdict = {"action": "accept", "_meta": {"native_delivery_attempt": "second"}}
        responses = await asyncio.gather(
            client.post(resolve_url, json=verdict),
            client.post(resolve_url, json=verdict),
        )
        assert all(response.status_code == 202 for response in responses)
        assert (await retry).json()["action"] == "accept"
        await _wait_for_state(eid, "sending")
    finally:
        retry.cancel()
        await asyncio.gather(retry, return_exceptions=True)

    # A late receipt during the gap between hook calls clears the same card.
    confirmed = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "external_elicitation_resolved",
            "data": {"elicitation_id": eid},
        },
    )
    assert confirmed.status_code == 202
    assert pending_elicitations.lookup(eid) is None
    payload["native_delivery"] = {"state": "unconfirmed", "attempt": "third", "action": "accept"}
    async with asyncio.timeout(5):
        resolved = await client.post(hook_url, json=payload)
    assert resolved.status_code == 200
    assert resolved.content == b""
    assert pending_elicitations.lookup(eid) is None


async def test_kiro_terminal_receipt_cancels_a_waiting_retry(client: httpx.AsyncClient) -> None:
    agent = await create_test_agent(client, "kiro-late-receipt")
    session_id = await create_session_for_agent(client, agent["id"])
    eid = f"elicit_kiro_{session_id}"
    task = asyncio.create_task(
        client.post(
            f"/v1/sessions/{session_id}/hooks/native-permission-request",
            json={
                "elicitation_id": eid,
                "agent": "Kiro",
                "policy_name": "kiro_native_permission",
                "native_delivery": {
                    "state": "unconfirmed",
                    "attempt": "retry",
                    "action": "accept",
                },
            },
        )
    )
    try:
        await _wait_for_state(eid, "unconfirmed")
        await client.post(
            f"/v1/sessions/{session_id}/events",
            json={
                "type": "external_elicitation_resolved",
                "data": {"elicitation_id": eid},
            },
        )
        async with asyncio.timeout(5):
            response = await task
        assert response.content == b""
        assert pending_elicitations.lookup(eid) is None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_kiro_child_delivery_stays_pending_in_parent_until_receipt(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    agent = await create_test_agent(client, "kiro-child-delivery")
    parent = await create_session_for_agent(client, agent["id"])
    child = (
        SqlAlchemyConversationStore(db_uri)
        .create_conversation(
            kind="sub_agent",
            title="Kiro:approval",
            parent_conversation_id=parent,
            agent_id=agent["id"],
        )
        .id
    )
    eid = f"elicit_kiro_{child}"
    hook = asyncio.create_task(
        client.post(
            f"/v1/sessions/{child}/hooks/native-permission-request",
            json={
                "elicitation_id": eid,
                "agent": "Kiro",
                "policy_name": "kiro_native_permission",
                "native_delivery": {"state": "ready", "attempt": "first"},
            },
        )
    )

    async def parent_delivery(state: str) -> dict:
        async with asyncio.timeout(5):
            while True:
                snapshot = (await client.get(f"/v1/sessions/{parent}")).json()
                entries = snapshot["pending_elicitations"]
                if entries and entries[0]["params"]["native_delivery"]["state"] == state:
                    return entries[0]
                await asyncio.sleep(0.01)

    try:
        mirrored = await parent_delivery("ready")
        assert mirrored["params"]["target_session_id"] == child
        wrong_owner = await client.post(
            f"/v1/sessions/{parent}/elicitations/{eid}/resolve", json={"action": "accept"}
        )
        assert wrong_owner.status_code == 202
        assert (await parent_delivery("ready"))["params"]["target_session_id"] == child
        assert not hook.done()
        response = await client.post(
            f"/v1/sessions/{child}/elicitations/{eid}/resolve", json={"action": "accept"}
        )
        assert response.status_code == 202
        assert (await hook).json()["action"] == "accept"
        assert (await parent_delivery("sending"))["params"]["target_session_id"] == child
        await client.post(
            f"/v1/sessions/{child}/events",
            json={"type": "external_elicitation_resolved", "data": {"elicitation_id": eid}},
        )
        snapshot = (await client.get(f"/v1/sessions/{parent}")).json()
        assert snapshot["pending_elicitations"] == []
    finally:
        hook.cancel()
        await asyncio.gather(hook, return_exceptions=True)


@pytest.mark.parametrize(
    "delivery",
    [
        {"state": [], "attempt": "retry"},
        {"state": "unconfirmed", "attempt": "retry", "action": {}},
        {"state": "sending", "attempt": "retry", "action": "accept"},
        {"state": "ready", "attempt": ""},
    ],
)
async def test_invalid_kiro_delivery_metadata_is_rejected(
    client: httpx.AsyncClient, delivery: dict
) -> None:
    agent = await create_test_agent(client, "kiro-invalid-delivery")
    sid = await create_session_for_agent(client, agent["id"])
    response = await client.post(
        f"/v1/sessions/{sid}/hooks/native-permission-request",
        json={
            "agent": "Kiro",
            "policy_name": "kiro_native_permission",
            "native_delivery": delivery,
        },
    )
    assert response.status_code == 400
