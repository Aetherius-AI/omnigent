"""A local listener collision must not prevent a real native Codex turn."""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

import httpx
import pytest
import yaml

from tests._helpers.server_runner import server_runner
from tests._helpers.session import bind_session_runner, bundle_files, post_session_bundle
from tests.e2e.conftest import configure_mock_llm

_RUNNER_BOOTSTRAP = """
import asyncio, json, os
from pathlib import Path
from urllib.parse import urlsplit
from omnigent.harnesses.codex_native.app_server import CodexNativeAppServer

original_start = CodexNativeAppServer.start

async def reject(reader, writer):
    writer.close()
    await writer.wait_closed()

async def collide(self):
    url = urlsplit(self.listen_url or "")
    if url.scheme != "ws":
        return await original_start(self)
    other = await asyncio.start_server(reject, url.hostname, url.port)
    try:
        await original_start(self)
        assert other.is_serving()
        Path(os.environ['KPI3_COLLISION_TRACE']).write_text(json.dumps({
            'occupied_port': url.port, 'ready_url': self.listen_url,
        }))
    finally:
        other.close()
        await other.wait_closed()

CodexNativeAppServer.start = collide
from omnigent.runner._entry import main
main()
"""


@pytest.mark.skipif(
    shutil.which("codex") is None or shutil.which("tmux") is None,
    reason="requires native Codex and tmux",
)
@pytest.mark.timeout(150)
def test_native_turn_survives_an_allocated_port_being_taken(
    tmp_path: Path, isolated_mock_llm_server_url: str
) -> None:
    model = "gpt-5.4"
    answer = "COLLISION_RECOVERED"
    configure_mock_llm(isolated_mock_llm_server_url, [{"text": answer}], key=model)
    source = tmp_path / "source-home"
    source.mkdir()
    trace = tmp_path / "collision.json"
    runtime = {"OMNIGENT_HARNESS_TMP_PARENT": os.environ.get("OMNIGENT_HARNESS_TMP_PARENT")}
    with server_runner(tmp_path / "stack", server_env=runtime) as stack:
        stack.start_runner(
            bootstrap=_RUNNER_BOOTSTRAP,
            env={
                **runtime,
                "CODEX_HOME": str(source),
                "KPI3_COLLISION_TRACE": str(trace),
            },
        )
        with httpx.Client(base_url=stack.base_url, trust_env=False, timeout=30) as client:
            created = post_session_bundle(
                client.post,
                "/v1/sessions",
                bundle_files(
                    {
                        "native.yaml": yaml.safe_dump(
                            {
                                "name": "collision-test",
                                "prompt": "Reply briefly.",
                                "executor": {
                                    "harness": "codex-native",
                                    "model": model,
                                    "auth": {
                                        "type": "api_key",
                                        "api_key": "mock-key",
                                        "base_url": f"{isolated_mock_llm_server_url}/v1",
                                    },
                                },
                            }
                        ).encode(),
                    }
                ),
            )
            created.raise_for_status()
            session = created.json()["session_id"]
            bind_session_runner(client.patch, stack.base_url, session, stack.runner_id)
            sent = client.post(
                f"/v1/sessions/{session}/events",
                json={
                    "type": "message",
                    "data": {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "Reply once."},
                        ],
                    },
                },
            )
            assert sent.is_success, (sent.status_code, sent.text, stack.log_tail())
            deadline = time.monotonic() + 90
            while True:
                response = client.get(f"/v1/sessions/{session}")
                response.raise_for_status()
                snapshot = response.json()
                assert snapshot["status"] != "failed", (
                    snapshot["last_task_error"],
                    stack.log_tail(),
                )
                if snapshot["status"] == "idle" and answer in json.dumps(snapshot["items"]):
                    break
                assert time.monotonic() < deadline, stack.log_tail()
                time.sleep(0.1)
            assert snapshot["last_task_error"] is None
            assert snapshot["pending_inputs"] == []
            recovery = json.loads(trace.read_text())
            assert not recovery["ready_url"].endswith(f":{recovery['occupied_port']}")
