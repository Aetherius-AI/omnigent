"""An old host's exit report does not fail idle work after a server restart."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import httpx
import pytest
import yaml
from websockets.asyncio.client import connect

from omnigent.host.frames import HostHelloFrame, HostRunnerExitedFrame, encode_host_frame
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests._helpers.live_server import terminate_process
from tests._helpers.server_runner import server_runner
from tests._helpers.session import bind_session_runner, bundle_files, post_session_bundle
from tests.e2e.conftest import configure_mock_llm
from tests.e2e.test_runner_tunnel_mid_turn_reconnect_grace_e2e import _send_user_message

# An older host that never owned the runner; its exit report carries no ``had_connected``.
_OLDER_HOST_ID = "7b778cb70b4b466f8676ad75aa21e8b6"


@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_idle_runner_exit_keeps_connection_history_after_restart(
    tmp_path: Path, isolated_mock_llm_server_url: str
) -> None:
    runtime_log = tmp_path / "server-runtime.log"
    model = "runner-history-mock"
    answer = "IDLE_HISTORY_COMPLETE"
    configure_mock_llm(isolated_mock_llm_server_url, [{"text": answer}], key=model)
    runtime = {"OMNIGENT_HARNESS_TMP_PARENT": os.environ.get("OMNIGENT_HARNESS_TMP_PARENT")}
    with server_runner(
        tmp_path / "stack", server_env={**runtime, "OMNIGENT_PROCESS_LOG_FILE": str(runtime_log)}
    ) as stack:
        stack.start_runner(env=runtime)
        store = SqlAlchemyConversationStore(stack.database_uri)
        try:
            with httpx.Client(base_url=stack.base_url, trust_env=False) as client:
                created = post_session_bundle(
                    client.post,
                    "/v1/sessions",
                    bundle_files(
                        {
                            "idle.yaml": yaml.safe_dump(
                                {
                                    "name": "idle",
                                    "prompt": "Reply briefly.",
                                    "executor": {
                                        "harness": "openai-agents",
                                        "model": model,
                                        "auth": {
                                            "type": "api_key",
                                            "api_key": "mock-key",
                                            "base_url": f"{isolated_mock_llm_server_url}/v1",
                                        },
                                    },
                                }
                            ).encode()
                        }
                    ),
                )
                created.raise_for_status()
                session_id = created.json()["session_id"]
                bind_session_runner(client.patch, stack.base_url, session_id, stack.runner_id)
                deadline = time.monotonic() + 15
                while True:
                    bound = store.get_runner_liveness(session_id)
                    if bound is not None and bound[1] is not None:
                        break
                    assert time.monotonic() < deadline, stack.log_tail()
                    await asyncio.sleep(0.05)
                _send_user_message(client, session_id, "Reply once, then wait for more input.")
                deadline = time.monotonic() + 30
                while True:
                    response = client.get(f"/v1/sessions/{session_id}")
                    response.raise_for_status()
                    completed = response.json()
                    assert completed["status"] != "failed", completed
                    if completed["status"] == "idle" and answer in str(completed["items"]):
                        break
                    assert time.monotonic() < deadline, stack.log_tail()
                    await asyncio.sleep(0.05)
                assert completed["last_task_error"] is None

            terminate_process(stack.runner)
            stack.runner = None
            deadline = time.monotonic() + 10
            while True:
                liveness = store.get_runner_liveness(session_id)
                if liveness is None or liveness[1] is None:
                    break
                assert time.monotonic() < deadline, stack.log_tail()
                await asyncio.sleep(0.05)
            stack.restart_server()

            marker = f"Runner disconnect for session={session_id}:"

            def decision_lines() -> list[str]:
                log = runtime_log.read_text(errors="replace") if runtime_log.exists() else ""
                return [line for line in log.splitlines() if marker in line]

            # The log survives the restart, so wait for a decision newer than any the
            # first server recorded when the runner was terminated.
            baseline = len(decision_lines())
            socket_url = stack.base_url.replace("http://", "ws://") + (
                f"/v1/hosts/{_OLDER_HOST_ID}/tunnel"
            )
            async with connect(socket_url) as socket:
                await socket.send(
                    encode_host_frame(
                        HostHelloFrame(
                            version="0.1-test", frame_protocol_version=1, name="Older host"
                        )
                    )
                )
                await socket.send(
                    encode_host_frame(
                        HostRunnerExitedFrame(
                            runner_id=stack.runner_id, error="Synthetic idle exit"
                        )
                    )
                )
                async with asyncio.timeout(10):
                    while len(decision_lines()) <= baseline:
                        await asyncio.sleep(0.05)
                assert "idle_no_failure" in decision_lines()[-1]
                async with httpx.AsyncClient(base_url=stack.base_url, trust_env=False) as client:
                    response = await client.get(f"/v1/sessions/{session_id}")
                    response.raise_for_status()
                    snapshot = response.json()
                assert snapshot["status"] == "idle", snapshot
                assert snapshot["last_task_error"] is None
                assert snapshot["runner_online"] is False
                persisted = store.get_conversation(session_id)
                assert persisted is not None and persisted.live_status == "idle"
                assert not persisted.labels.get("omnigent.last_task_error_code")
        finally:
            store._engine.dispose()
