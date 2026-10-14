"""Startup may race another local process for an automatically chosen port."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from omnigent.harnesses.codex_native.app_server import CodexNativeAppServer


@pytest.fixture
def fake_codex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    source = tmp_path / "shared-codex-home"
    source.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(source))
    executable = tmp_path / "fake-codex"
    executable.write_text(
        f"#!{sys.executable}\n"
        + """
import asyncio, json, os, sys
from urllib.parse import urlsplit
from websockets.asyncio.server import serve

if "--version" in sys.argv:
    print("codex-cli 0.128.0")
    raise SystemExit(0)

url = urlsplit(sys.argv[sys.argv.index("--listen") + 1])
with open(os.environ["KPI3_TEST_PORTS_FILE"], "a") as record:
    record.write(str(url.port) + "\\n")

async def handle(ws):
    async for raw in ws:
        request = json.loads(raw)
        if "id" in request:
            await ws.send(json.dumps({"id": request["id"], "result": {}}))

async def main():
    async with serve(handle, url.hostname, url.port):
        await asyncio.Future()

try:
    asyncio.run(main())
except OSError as error:
    print("Error: Address in use (os error 98)", file=sys.stderr, flush=True)
    raise SystemExit(1)
""",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    return executable


async def test_automatic_port_collision_restarts_only_its_owned_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_codex: Path
) -> None:
    async def reject(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()
        await writer.wait_closed()

    ports_file = tmp_path / "attempted-ports"
    foreign = await asyncio.start_server(reject, "127.0.0.1", 0)
    occupied = foreign.sockets[0].getsockname()[1]
    app = CodexNativeAppServer(
        codex_path=str(fake_codex),
        socket_path=tmp_path / "unused.sock",
        codex_home=tmp_path / "private-home",
        cwd=tmp_path,
        bridge_dir=tmp_path / "bridge",
        config_overrides=[],
        env={**os.environ, "KPI3_TEST_PORTS_FILE": str(ports_file)},
        listen_url=f"ws://127.0.0.1:{occupied}",
        reconcile_process_registry=False,
    )
    app.retry_bind_collision = True
    try:
        await app.start()
        assert app.listen_url != f"ws://127.0.0.1:{occupied}"
        assert app.proc is not None and app.proc.returncode is None
        attempted = [int(line) for line in ports_file.read_text().splitlines()]
        assert len(attempted) == 2 and attempted[0] == occupied
        assert foreign.is_serving()
    finally:
        await app.close()
        foreign.close()
        await foreign.wait_closed()


async def test_explicit_listener_does_not_move_after_a_collision(
    tmp_path: Path, fake_codex: Path
) -> None:
    async def reject(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()
        await writer.wait_closed()

    foreign = await asyncio.start_server(reject, "127.0.0.1", 0)
    url = f"ws://127.0.0.1:{foreign.sockets[0].getsockname()[1]}"
    app = CodexNativeAppServer(
        codex_path=str(fake_codex),
        socket_path=tmp_path / "unused.sock",
        codex_home=tmp_path / "private-home",
        cwd=tmp_path,
        bridge_dir=tmp_path / "bridge",
        config_overrides=[],
        env={**os.environ, "KPI3_TEST_PORTS_FILE": str(tmp_path / "attempted-ports")},
        listen_url=url,
        reconcile_process_registry=False,
    )
    try:
        with pytest.raises(RuntimeError, match="Address in use"):
            await app.start()
        assert app.listen_url == url
        assert app.proc is None
        assert foreign.is_serving()
    finally:
        await app.close()
        foreign.close()
        await foreign.wait_closed()


async def test_repeated_collisions_stop_after_three_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_codex: Path
) -> None:
    from omnigent.harnesses.codex_native import app_server

    async def reject(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()
        await writer.wait_closed()

    foreign = await asyncio.start_server(reject, "127.0.0.1", 0)
    port = foreign.sockets[0].getsockname()[1]
    monkeypatch.setattr(app_server, "_allocate_loopback_port", lambda: port)
    recorded = tmp_path / "attempted-ports"
    app = CodexNativeAppServer(
        codex_path=str(fake_codex),
        socket_path=tmp_path / "unused.sock",
        codex_home=tmp_path / "private-home",
        cwd=tmp_path,
        bridge_dir=tmp_path / "bridge",
        config_overrides=[],
        env={**os.environ, "KPI3_TEST_PORTS_FILE": str(recorded)},
        listen_url=f"ws://127.0.0.1:{port}",
        reconcile_process_registry=False,
        retry_bind_collision=True,
    )
    try:
        with pytest.raises(RuntimeError, match="Address in use"):
            await app.start()
        assert recorded.read_text().splitlines() == [str(port)] * 3
        assert app.proc is None and app.process_owner_lock is None
        assert foreign.is_serving()
    finally:
        await app.close()
        foreign.close()
        await foreign.wait_closed()
