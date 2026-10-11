"""Guards for the Windows degraded-mode links that need no Windows host: native
bridges import without ``os.getuid``, workspace validation accepts drive-letter
paths, and the daemon env forwards an explicit ``PYTHONUTF8``.
"""

from __future__ import annotations

import subprocess
import sys

import pytest


def test_native_bridges_import_without_os_getuid() -> None:
    """Native bridges must import when os.getuid is unavailable."""
    child = (
        "import os\n"
        "delattr(os, 'getuid')\n"
        "import omnigent.harnesses.kiro_native.bridge\n"
        "import omnigent.harnesses.hermes_native.bridge\n"
        "import omnigent.harnesses.kimi_native.bridge\n"
        "import omnigent.harnesses.qwen_native.bridge\n"
        "print('BRIDGES_IMPORT_OK')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", child],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, (
        f"native bridge import requires os.getuid (server startup would crash "
        f"on Windows):\n{proc.stderr}"
    )
    assert "BRIDGES_IMPORT_OK" in proc.stdout


def test_workspace_validation_accepts_windows_drive_paths() -> None:
    """Windows drive-absolute paths must pass workspace validation."""
    import asyncio

    from omnigent.server.routes._workspace_validation import (
        WorkspaceValidationError,
        validate_workspace,
    )

    class _EmptyRegistry:
        def get(self, host_id: str) -> None:
            return None

    def _rejection(workspace: str) -> str:
        try:
            asyncio.run(
                validate_workspace(
                    host_registry=_EmptyRegistry(),
                    host_id="host_missing",
                    workspace=workspace,
                    spec_cwd=None,
                )
            )
        except WorkspaceValidationError as exc:
            return str(exc)
        return ""

    for workspace in ("D:\\myproject", "C:\\Users\\dev\\proj", "C:/Users/dev/proj"):
        message = _rejection(workspace)
        assert "absolute path" not in message, (
            f"absolute Windows workspace rejected as non-absolute: {workspace!r}: {message}"
        )
        assert "offline" in message, f"expected the offline-host failure, got: {message!r}"
    for workspace in ("myproject", "relative\\path"):
        message = _rejection(workspace)
        assert "absolute path" in message, (
            f"relative workspace passed the absoluteness check: {workspace!r}: {message!r}"
        )


def test_host_daemon_env_preserves_pythonutf8(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The user's UTF-8 mode must reach local and remote host daemons."""
    from omnigent.cli import _build_host_daemon_env

    monkeypatch.setenv("PYTHONUTF8", "1")
    remote_env = _build_host_daemon_env(server_url="https://server.example")
    local_env = _build_host_daemon_env(server_url=None)
    assert remote_env.get("PYTHONUTF8") == "1", (
        "PYTHONUTF8 stripped from the remote host-daemon env"
    )
    assert local_env.get("PYTHONUTF8") == "1", "PYTHONUTF8 stripped from the local host-daemon env"
