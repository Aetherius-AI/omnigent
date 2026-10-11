"""Native harness bridges must import on a host without ``os.getuid``.

A module-level ``os.getuid`` lookup in a bridge would crash host/server startup
on Windows, where that attribute does not exist.
"""

from __future__ import annotations

import subprocess
import sys


def test_native_bridges_import_without_os_getuid() -> None:
    """Native bridges must import when os.getuid is unavailable."""
    child = (
        "import os\n"
        "if hasattr(os, 'getuid'): delattr(os, 'getuid')\n"
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
