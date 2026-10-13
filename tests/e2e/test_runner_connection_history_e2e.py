"""An old host's exit report does not fail idle work after a server restart."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import httpx
import pytest
from websockets.asyncio.client import connect

from omnigent.host.frames import HostHelloFrame, HostRunnerExitedFrame, encode_host_frame
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests._helpers.live_server import terminate_process
from tests._helpers.server_runner import server_runner
from tests._helpers.session import bind_session_runner, bundle_files, post_session_bundle


@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_idle_runner_exit_keeps_connection_history_after_restart(tmp_path: Path) -> None:
    runtime_log = tmp_path / "server-runtime.log"
    with server_runner(
        tmp_path / "stack", server_env={"OMNIGENT_PROCESS_LOG_FILE": str(runtime_log)}
    ) as stack:
        stack.start_runner()
        with httpx.Client(base_url=stack.base_url, trust_env=False) as client:
            created = post_session_bundle(
                client.post,
                "/v1/sessions",
                bundle_files(
                    {
                        "idle.yaml": (
                            b"name: idle\nprompt: Stay idle.\n"
                            b"executor:\n  harness: openai-agents\n"
                        )
                    }
                ),
            )
            created.raise_for_status()
            session_id = created.json()["session_id"]
            bind_session_runner(client.patch, stack.base_url, session_id, stack.runner_id)
            store = SqlAlchemyConversationStore(stack.database_uri)
            deadline = time.monotonic() + 15
            while True:
                bound = store.get_runner_liveness(session_id)
                if bound is not None and bound[1] is not None:
                    break
                assert time.monotonic() < deadline, stack.log_tail()
                await asyncio.sleep(0.05)
            observed = client.post(
                f"/v1/sessions/{session_id}/events",
                json={"type": "external_session_status", "data": {"status": "idle"}},
            )
            observed.raise_for_status()

        terminate_process(stack.runner)
        stack.runner = None
        deadline = time.monotonic() + 10
        while store.get_runner_liveness(session_id)[1] is not None:
            assert time.monotonic() < deadline, stack.log_tail()
            await asyncio.sleep(0.05)
        stack.restart_server()

        socket_url = stack.base_url.replace("http://", "ws://") + (
            "/v1/hosts/7b778cb70b4b466f8676ad75aa21e8b6/tunnel"
        )
        async with connect(socket_url) as socket:
            await socket.send(
                encode_host_frame(
                    HostHelloFrame(version="0.1-test", frame_protocol_version=1, name="Owned host")
                )
            )
            await socket.send(
                encode_host_frame(
                    HostRunnerExitedFrame(runner_id=stack.runner_id, error="Synthetic idle exit")
                )
            )
            async with asyncio.timeout(10):
                while True:
                    decisions = [
                        line
                        for line in runtime_log.read_text(errors="replace").splitlines()
                        if f"Runner disconnect for session={session_id}:" in line
                    ]
                    if decisions:
                        break
                    await asyncio.sleep(0.05)
            assert "idle_no_failure" in decisions[-1]
            async with httpx.AsyncClient(base_url=stack.base_url, trust_env=False) as client:
                response = await client.get(f"/v1/sessions/{session_id}")
                response.raise_for_status()
                snapshot = response.json()
            assert snapshot["status"] == "idle"
            assert snapshot["last_task_error"] is None
            assert snapshot["runner_online"] is False
            persisted = store.get_conversation(session_id)
            assert persisted is not None and persisted.live_status == "idle"
            assert "omnigent.last_task_error_code" not in persisted.labels
