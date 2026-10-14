"""Startup tests for Codex app server."""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import Mock

import psutil
import pytest

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.harnesses.codex_egress import CertificateFailure
from omnigent.harnesses.codex_native.app_server import (
    CodexAppServerClient,
    CodexNativeAppServer,
)
from omnigent.harnesses.codex_native.bridge import (
    read_certificate_failure,
    record_certificate_failure,
)
from tests.harnesses.codex_native.app_server._support import (
    _disable_codex_startup_rpc,
    _FakeStartupClient,
    _test_app_server,
)


async def test_start_reuses_initialized_readiness_client_for_hook_trust(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Startup trusts hooks over the readiness connection, then closes it."""
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server

    source_home = tmp_path / "source-codex-home"
    source_home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(source_home))

    async def _supported_version(_codex_path: str) -> tuple[int, int, int]:
        return (0, 147, 0)

    startup_client = _FakeStartupClient()
    trusted_with: list[object] = []

    async def _ready(_self: CodexNativeAppServer) -> _FakeStartupClient:
        return startup_client

    async def _trust(
        _self: CodexNativeAppServer, *, client: CodexAppServerClient | None = None
    ) -> None:
        trusted_with.append(client)

    monkeypatch.setattr(codex_native_app_server, "_codex_cli_version", _supported_version)
    monkeypatch.setattr(CodexNativeAppServer, "_wait_until_ready", _ready)
    monkeypatch.setattr(CodexNativeAppServer, "_trust_policy_hooks", _trust)
    server = _test_app_server(
        tmp_path,
        tmp_path / "codex-home",
        tmp_path / "bridge",
        workspace,
    )

    await server.start()
    try:
        assert trusted_with == [startup_client]
        assert startup_client.close_calls == 1
    finally:
        await server.close()


async def test_start_cancellation_closes_reused_client_and_app_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancellation during hook trust closes both startup resources."""
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server

    source_home = tmp_path / "source-codex-home"
    source_home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(source_home))

    async def _supported_version(_codex_path: str) -> tuple[int, int, int]:
        return (0, 147, 0)

    startup_client = _FakeStartupClient()
    trust_started = asyncio.Event()

    async def _ready(_self: CodexNativeAppServer) -> _FakeStartupClient:
        return startup_client

    async def _trust(
        _self: CodexNativeAppServer, *, client: CodexAppServerClient | None = None
    ) -> None:
        assert client is startup_client
        trust_started.set()
        await asyncio.Future()

    monkeypatch.setattr(codex_native_app_server, "_codex_cli_version", _supported_version)
    monkeypatch.setattr(CodexNativeAppServer, "_wait_until_ready", _ready)
    monkeypatch.setattr(CodexNativeAppServer, "_trust_policy_hooks", _trust)
    server = _test_app_server(
        tmp_path,
        tmp_path / "codex-home",
        tmp_path / "bridge",
        workspace,
    )

    task = asyncio.create_task(server.start())
    await trust_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert startup_client.close_calls == 1
    assert server.proc is None
    assert server.stderr_task is None


async def test_wait_until_ready_closes_failed_client_before_reconnect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused readiness attempt is closed before returning its retry."""
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server

    @dataclass
    class _ProbeClient:
        fail_connect: bool
        connect_calls: int = 0
        close_calls: int = 0

        async def connect(self) -> None:
            self.connect_calls += 1
            if self.fail_connect:
                raise OSError("listener not ready")

        async def close(self) -> None:
            self.close_calls += 1

    first = _ProbeClient(fail_connect=True)
    second = _ProbeClient(fail_connect=False)
    attempts = [first, second]

    def _client(*_args: object, **_kwargs: object) -> _ProbeClient:
        return attempts.pop(0)

    async def _no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(codex_native_app_server, "CodexAppServerClient", _client)
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    server = _test_app_server(
        tmp_path,
        tmp_path / "codex-home",
        tmp_path / "bridge",
        workspace,
    )
    server.proc = Mock(returncode=None)

    connected = await server._wait_until_ready()

    assert connected is second
    assert first.connect_calls == 1
    assert first.close_calls == 1
    assert second.connect_calls == 1
    assert second.close_calls == 0


async def test_wait_until_ready_cancellation_closes_connecting_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancellation during initialize closes the half-open startup client."""
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server

    connect_started = asyncio.Event()

    @dataclass
    class _ConnectingClient:
        close_calls: int = 0

        async def connect(self) -> None:
            connect_started.set()
            await asyncio.Future()

        async def close(self) -> None:
            self.close_calls += 1

    client = _ConnectingClient()
    monkeypatch.setattr(
        codex_native_app_server,
        "CodexAppServerClient",
        lambda *_args, **_kwargs: client,
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    server = _test_app_server(
        tmp_path,
        tmp_path / "codex-home",
        tmp_path / "bridge",
        workspace,
    )
    server.proc = Mock(returncode=None)

    task = asyncio.create_task(server._wait_until_ready())
    await connect_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert client.close_calls == 1


async def test_wait_until_ready_deadline_is_the_app_server_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A listener ready after the discovery budget but inside the app-server budget succeeds."""
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server

    loop = asyncio.get_running_loop()
    real_time = loop.time
    clock = {"offset": 0.0}
    monkeypatch.setattr(loop, "time", lambda: real_time() + clock["offset"])
    # Each refused probe costs 5 s of virtual time, so the listener accepts after
    # 20 s: past the 10 s discovery budget, inside the 60 s app-server budget.
    ready_at = loop.time() + 20.0

    @dataclass
    class _SlowListenerClient:
        close_calls: int = 0

        async def connect(self) -> None:
            if loop.time() < ready_at:
                clock["offset"] += 5.0
                raise OSError("[Errno 111] Connect call failed ('127.0.0.1', 57045)")

        async def close(self) -> None:
            self.close_calls += 1

    clients: list[_SlowListenerClient] = []

    def _client(*_args: object, **_kwargs: object) -> _SlowListenerClient:
        clients.append(_SlowListenerClient())
        return clients[-1]

    monkeypatch.setattr(codex_native_app_server, "CodexAppServerClient", _client)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    server = _test_app_server(
        tmp_path,
        tmp_path / "codex-home",
        tmp_path / "bridge",
        workspace,
    )
    server.listen_url = "ws://127.0.0.1:57045"
    server.proc = Mock(returncode=None)

    connected = await server._wait_until_ready()

    assert connected is clients[-1]
    assert connected.close_calls == 0
    assert all(client.close_calls == 1 for client in clients[:-1])
    assert clock["offset"] > codex_native_app_server._CONNECT_TIMEOUT_SECONDS
    assert clock["offset"] < codex_native_app_server._APP_SERVER_READY_TIMEOUT_SECONDS


@pytest.mark.parametrize("listen_url", ["ws://127.0.0.1:57045", None], ids=["ws", "unix"])
async def test_wait_until_ready_timeout_reports_listen_target_and_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, listen_url: str | None
) -> None:
    """A listener that never accepts fails after the app-server budget, naming the target."""
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server

    loop = asyncio.get_running_loop()
    real_time = loop.time
    clock = {"offset": 0.0}
    monkeypatch.setattr(loop, "time", lambda: real_time() + clock["offset"])

    @dataclass
    class _RefusingClient:
        close_calls: int = 0

        async def connect(self) -> None:
            # Each refused probe costs 25 s of virtual time: three attempts
            # exhaust the 60 s budget without a real wait.
            clock["offset"] += 25.0
            raise OSError("[Errno 111] Connect call failed ('127.0.0.1', 57045)")

        async def close(self) -> None:
            self.close_calls += 1

    clients: list[_RefusingClient] = []

    def _client(*_args: object, **_kwargs: object) -> _RefusingClient:
        clients.append(_RefusingClient())
        return clients[-1]

    monkeypatch.setattr(codex_native_app_server, "CodexAppServerClient", _client)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    server = _test_app_server(
        tmp_path,
        tmp_path / "codex-home",
        tmp_path / "bridge",
        workspace,
    )
    server.listen_url = listen_url
    server.proc = Mock(returncode=None)

    with pytest.raises(RuntimeError) as excinfo:
        await server._wait_until_ready()

    message = str(excinfo.value)
    budget = codex_native_app_server._APP_SERVER_READY_TIMEOUT_SECONDS
    target = listen_url or f"unix://{server.socket_path}"
    assert message.startswith(
        f"Timed out after {budget:g}s waiting for the Codex app-server at {target}: "
    )
    assert "Connect call failed" in message
    assert (str(server.socket_path) in message) is (listen_url is None)
    assert len(clients) == 3
    assert all(client.close_calls == 1 for client in clients)


async def test_standalone_hook_trust_closes_client_when_connect_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed standalone trust handshake closes its partially-open client."""
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server

    @dataclass
    class _FailingClient:
        close_calls: int = 0

        async def connect(self) -> None:
            raise RuntimeError("initialize failed")

        async def close(self) -> None:
            self.close_calls += 1

    client = _FailingClient()
    monkeypatch.setattr(
        codex_native_app_server,
        "CodexAppServerClient",
        lambda *_args, **_kwargs: client,
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    server = _test_app_server(
        tmp_path,
        tmp_path / "codex-home",
        tmp_path / "bridge",
        workspace,
    )

    with pytest.raises(RuntimeError, match="initialize failed"):
        await server._trust_policy_hooks()

    assert client.close_calls == 1


async def test_start_can_delegate_global_process_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runner-owned Codex startup leaves global cleanup to the host janitor."""
    real_codex_home = tmp_path / "real-codex-home"
    real_codex_home.mkdir()
    codex_home = tmp_path / "codex-home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(real_codex_home))
    _disable_codex_startup_rpc(monkeypatch)
    reconcile_calls = 0

    def _record_reconcile() -> int:
        nonlocal reconcile_calls
        reconcile_calls += 1
        return 0

    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.app_server.reconcile_codex_native_process_registry",
        _record_reconcile,
    )
    server = _test_app_server(
        tmp_path,
        codex_home,
        tmp_path / "bridge",
        workspace,
    )
    server.reconcile_process_registry = False

    await server.start()
    await server.close()

    assert reconcile_calls == 0


async def test_start_clears_previous_launch_certificate_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A certificate record left by an earlier launch must not fail this launch's turns."""
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server

    source_home = tmp_path / "source-codex-home"
    source_home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(source_home))

    async def _supported_version(_codex_path: str) -> tuple[int, int, int]:
        return (0, 147, 0)

    monkeypatch.setattr(codex_native_app_server, "_codex_cli_version", _supported_version)
    _disable_codex_startup_rpc(monkeypatch)
    record_certificate_failure(
        bridge_dir, CertificateFailure(evidence="certificate expired", expired=True)
    )
    server = _test_app_server(tmp_path, tmp_path / "codex-home", bridge_dir, workspace)

    await server.start()
    try:
        assert read_certificate_failure(bridge_dir) is None
    finally:
        await server.close()


async def test_start_reports_a_removed_workspace_before_any_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deleted worktree fails as WORKSPACE_MISSING instead of a bare spawn ENOENT."""
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server

    source_home = tmp_path / "source-codex-home"
    source_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(source_home))

    async def _supported_version(_codex_path: str) -> tuple[int, int, int]:
        return (0, 147, 0)

    monkeypatch.setattr(codex_native_app_server, "_codex_cli_version", _supported_version)
    _disable_codex_startup_rpc(monkeypatch)
    codex_home = tmp_path / "codex-home"
    server = _test_app_server(tmp_path, codex_home, tmp_path / "bridge", tmp_path / "removed")

    with pytest.raises(OmnigentError) as failure:
        await server.start()

    assert failure.value.code == ErrorCode.WORKSPACE_MISSING
    assert server.proc is None
    assert not codex_home.exists()


@pytest.fixture
def loopback_startup_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[CodexNativeAppServer, Path]:
    """Real process/JSON-RPC peer with caller-controlled startup failures."""
    source_home = tmp_path / "source-codex-home"
    source_home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(source_home))
    monkeypatch.setenv("OMNIGENT_CODEX_NATIVE_STATE_DIR", str(tmp_path / "state"))
    attempts = tmp_path / "attempts.jsonl"
    executable = tmp_path / "synthetic-codex"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import asyncio, json, os, sys, tomllib\n"
        "from pathlib import Path\n"
        "from urllib.parse import urlsplit\n"
        "from websockets.asyncio.server import serve\n"
        "if '--version' in sys.argv:\n"
        "    print('codex-cli 0.128.0'); sys.exit(0)\n"
        "url = sys.argv[sys.argv.index('--listen') + 1]\n"
        "config = tomllib.loads((Path(os.environ['CODEX_HOME']) / 'config.toml').read_text())\n"
        "provider = config.get('model_providers', {}).get('synthetic', {}).get('base_url')\n"
        f"with Path({str(attempts)!r}).open('a') as output:\n"
        "    output.write(json.dumps({'pid': os.getpid(), 'url': url, "
        "'provider': provider}) + '\\n')\n"
        "failure = os.environ.get('SYNTHETIC_CODEX_START_ERROR')\n"
        "if failure:\n"
        "    print(failure, file=sys.stderr); sys.exit(1)\n"
        "if os.environ.get('SYNTHETIC_CODEX_PAUSE_RETRY') and "
        f"len(Path({str(attempts)!r}).read_text().splitlines()) > 1:\n"
        "    asyncio.run(asyncio.sleep(3600)); sys.exit(0)\n"
        "async def handler(ws):\n"
        "    async for raw in ws:\n"
        "        request = json.loads(raw)\n"
        "        if 'id' in request:\n"
        "            await ws.send(json.dumps({'id': request['id'], 'result': {}}))\n"
        "async def main():\n"
        "    try:\n"
        "        async with serve(handler, '127.0.0.1', urlsplit(url).port):\n"
        "            await asyncio.Future()\n"
        "    except OSError as error:\n"
        "        print(f'Error: Address in use (os error {error.errno})', file=sys.stderr)\n"
        "        sys.exit(1)\n"
        "asyncio.run(main())\n"
    )
    executable.chmod(0o755)

    server = _test_app_server(tmp_path, tmp_path / "codex-home", tmp_path / "bridge", workspace)
    server.codex_path = str(executable)
    server.reconcile_process_registry = False
    return server, attempts


async def _reject_loopback_connection(
    _reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    writer.close()
    await writer.wait_closed()


@pytest.mark.posix_only
async def test_start_recovers_from_an_occupied_loopback_port(
    loopback_startup_server: tuple[CodexNativeAppServer, Path],
) -> None:
    """A real competing listener survives while the owned child moves ports."""
    server, attempts = loopback_startup_server
    foreign = await asyncio.start_server(_reject_loopback_connection, "127.0.0.1", 0)
    occupied_port = foreign.sockets[0].getsockname()[1]
    server.listen_url = f"ws://127.0.0.1:{occupied_port}"
    server.config_overrides = [
        'model_provider="synthetic"',
        'model_providers.synthetic={name="Synthetic", '
        'base_url="https://provider.example.test/v1", wire_api="responses"}',
    ]
    process = None
    try:
        await server.start()
        process = server.proc
        assert process is not None and process.returncode is None
        spawned = [json.loads(line) for line in attempts.read_text().splitlines()]
        assert len(spawned) == 2
        assert all(child["provider"] == "https://provider.example.test/v1" for child in spawned)
        assert spawned[0]["url"] == f"ws://127.0.0.1:{occupied_port}"
        assert spawned[1]["url"] == server.listen_url != spawned[0]["url"]
        assert not psutil.pid_exists(spawned[0]["pid"])
        assert foreign.is_serving()
        reader, writer = await asyncio.open_connection("127.0.0.1", occupied_port)
        writer.close()
        await writer.wait_closed()
        assert await reader.read() == b""
    finally:
        await server.close()
        foreign.close()
        await foreign.wait_closed()
    assert process is not None and process.returncode is not None
    assert server.proc is None and server.stderr_task is None


@pytest.mark.posix_only
async def test_start_stops_after_one_fresh_port_retry(
    loopback_startup_server: tuple[CodexNativeAppServer, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second actual collision remains a failure without touching either owner."""
    server, attempts = loopback_startup_server
    first = await asyncio.start_server(_reject_loopback_connection, "127.0.0.1", 0)
    second = await asyncio.start_server(_reject_loopback_connection, "127.0.0.1", 0)
    server.listen_url = f"ws://127.0.0.1:{first.sockets[0].getsockname()[1]}"
    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.app_server._allocate_loopback_port",
        lambda: second.sockets[0].getsockname()[1],
    )
    try:
        with pytest.raises(RuntimeError, match=r"Address .*in use") as failure:
            await server.start()
        assert type(failure.value) is RuntimeError
        assert len(attempts.read_text().splitlines()) == 2
        assert first.is_serving() and second.is_serving()
        assert server.proc is None and server.stderr_task is None
    finally:
        await server.close()
        first.close()
        second.close()
        await first.wait_closed()
        await second.wait_closed()


@pytest.mark.parametrize(
    "failure",
    ["Error: Model provider `missing` not found", "Error: Address in use in config"],
)
@pytest.mark.posix_only
async def test_start_preserves_other_owned_child_failures(
    loopback_startup_server: tuple[CodexNativeAppServer, Path], failure: str
) -> None:
    server, attempts = loopback_startup_server
    foreign = await asyncio.start_server(_reject_loopback_connection, "127.0.0.1", 0)
    endpoint = f"ws://127.0.0.1:{foreign.sockets[0].getsockname()[1]}"
    server.listen_url = endpoint
    server.env["SYNTHETIC_CODEX_START_ERROR"] = failure
    try:
        with pytest.raises(RuntimeError, match=failure.replace("`", "[`]")):
            await server.start()
        assert len(attempts.read_text().splitlines()) == 1
        assert server.listen_url == endpoint
        assert server.proc is None and server.stderr_task is None
        assert foreign.is_serving()
    finally:
        await server.close()
        foreign.close()
        await foreign.wait_closed()


@pytest.mark.posix_only
async def test_cancellation_during_fresh_port_retry_reaps_the_owned_child(
    loopback_startup_server: tuple[CodexNativeAppServer, Path],
) -> None:
    server, attempts = loopback_startup_server
    foreign = await asyncio.start_server(_reject_loopback_connection, "127.0.0.1", 0)
    server.listen_url = f"ws://127.0.0.1:{foreign.sockets[0].getsockname()[1]}"
    server.env["SYNTHETIC_CODEX_PAUSE_RETRY"] = "1"
    task = asyncio.create_task(server.start())
    try:
        async with asyncio.timeout(10):
            while not attempts.exists() or len(attempts.read_text().splitlines()) < 2:
                await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        spawned = [json.loads(line) for line in attempts.read_text().splitlines()]
        assert len(spawned) == 2
        assert all(not psutil.pid_exists(child["pid"]) for child in spawned)
        assert server.proc is None and server.stderr_task is None
        assert foreign.is_serving()
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        await server.close()
        foreign.close()
        await foreign.wait_closed()


@pytest.mark.parametrize("cleanup_cost", [0.0, 20.0])
async def test_collision_retry_shares_the_original_readiness_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cleanup_cost: float
) -> None:
    """A collision after 45 seconds leaves 15 seconds, rather than a new minute."""
    from types import SimpleNamespace

    from omnigent.harnesses.codex_native import app_server as module

    loop = asyncio.get_running_loop()
    real_time = loop.time
    offset = 0.0
    monkeypatch.setattr(loop, "time", lambda: real_time() + offset)
    attempts = 0
    connects = 0

    async def capture_exit() -> None:
        nonlocal offset
        offset += 45.0

    class RefusingClient:
        async def connect(self) -> None:
            nonlocal offset, connects
            connects += 1
            offset += 20.0
            raise OSError("listener not ready")

        async def close(self) -> None:
            pass

    async def spawn(self: CodexNativeAppServer) -> None:
        nonlocal attempts
        attempts += 1
        self.proc = SimpleNamespace(returncode=1 if attempts == 1 else None)
        self.recent_stderr = ["Error: Address in use (os error 98)"] if attempts == 1 else []
        self.stderr_task = asyncio.create_task(capture_exit()) if attempts == 1 else None

    original_close = module.CodexNativeAppServer.close

    async def close(self: CodexNativeAppServer) -> None:
        nonlocal offset
        await original_close(self)
        offset += cleanup_cost

    monkeypatch.setattr(module.CodexNativeAppServer, "close", close)
    monkeypatch.setattr(module.CodexNativeAppServer, "_spawn_process", spawn)
    monkeypatch.setattr(module, "CodexAppServerClient", lambda *args, **kwargs: RefusingClient())
    server = _test_app_server(tmp_path, tmp_path / "home", tmp_path / "bridge", tmp_path)
    server.listen_url = "ws://127.0.0.1:45001"
    expected_error = r"Address .*in use" if cleanup_cost else "Timed out after 60s"
    with pytest.raises(RuntimeError, match=expected_error):
        await server._start_owned_listener()
    assert attempts == (1 if cleanup_cost else 2)
    assert connects == (0 if cleanup_cost else 1)
    if cleanup_cost:
        assert server.proc is None
