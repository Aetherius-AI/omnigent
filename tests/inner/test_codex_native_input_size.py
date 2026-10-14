"""Large attachments must survive Codex's native text-input limit."""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path

import pytest
from websockets.asyncio.server import ServerConnection, serve

from omnigent.harnesses.codex_native.bridge import CodexNativeBridgeState, write_bridge_state
from omnigent.inner.codex_native_executor import CodexNativeExecutor, _content_to_input_items
from omnigent.inner.executor import ExecutorError, TurnComplete


def _file(text: str, name: str) -> dict[str, object]:
    return {
        "type": "input_file",
        "filename": name,
        "file_data": "data:text/plain;base64," + base64.b64encode(text.encode()).decode(),
    }


@pytest.mark.parametrize("texts", [("a" * 1_373_866,), ("a" * 600_000, "b" * 600_000)])
def test_large_text_files_fit_and_keep_the_complete_contents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, texts: tuple[str, ...]
) -> None:
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    content = [_file(text, f"report-{index}.txt") for index, text in enumerate(texts)]
    content.append({"type": "input_text", "text": "Compare the complete reports."})
    items = _content_to_input_items(content, tmp_path / "bridge")
    assert sum(len(str(item.get("text", ""))) for item in items) <= 1_048_576
    recovered = []
    for item in items[:-1]:
        text = str(item["text"])
        match = re.fullmatch(r"\[Attached(?: file)?: (.+)\]", text)
        recovered.append(Path(match[1]).read_text() if match else text)
    assert recovered == list(texts)
    assert items[-1] == {"type": "text", "text": "Compare the complete reports."}


async def test_large_file_is_accepted_by_a_real_rpc_peer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    original = "report row: café ☕\n" * 80_000
    received: list[str] = []

    async def handle(ws: ServerConnection) -> None:
        async for raw in ws:
            request = json.loads(raw)
            if "id" not in request:
                continue
            method = request["method"]
            result: dict[str, object] = {}
            if method == "turn/start":
                items = request["params"]["input"]
                size = sum(len(item.get("text", "")) for item in items)
                if size > 1_048_576:
                    await ws.send(
                        json.dumps(
                            {
                                "id": request["id"],
                                "error": {
                                    "code": -32602,
                                    "message": "Input exceeds the maximum length.",
                                    "data": {
                                        "input_error_code": "input_too_large",
                                        "max_chars": 1_048_576,
                                    },
                                },
                            }
                        )
                    )
                    continue
                for item in items:
                    match = re.fullmatch(r"\[Attached(?: file)?: (.+)\]", item.get("text", ""))
                    if match:
                        received.append(Path(match[1]).read_text())
                result = {"turn": {"id": "accepted-turn"}}
            await ws.send(json.dumps({"id": request["id"], "result": result}))

    async with serve(handle, "127.0.0.1", 0, max_size=None) as server:
        port = server.sockets[0].getsockname()[1]
        write_bridge_state(
            tmp_path,
            CodexNativeBridgeState(
                session_id="file-session",
                socket_path=f"ws://127.0.0.1:{port}",
                thread_id="file-thread",
                codex_home=str(tmp_path / "codex-home"),
            ),
        )
        executor = CodexNativeExecutor(bridge_dir=tmp_path)
        events = [
            event
            async for event in executor.run_turn(
                [{"role": "user", "content": [_file(original, "report.txt")]}],
                [],
                "",
            )
        ]
    assert len(events) == 1 and isinstance(events[0], TurnComplete)
    assert received == [original]


async def test_oversized_authored_text_is_retained_before_any_rpc(tmp_path: Path) -> None:
    executor = CodexNativeExecutor(bridge_dir=tmp_path)
    events = [
        event
        async for event in executor.run_turn(
            [{"role": "user", "content": "x" * 1_048_577}],
            [],
            "",
        )
    ]
    assert len(events) == 1 and isinstance(events[0], ExecutorError)
    assert events[0].undelivered is True
    assert "attach" in (events[0].remediation or "").lower()
