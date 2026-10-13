"""Deliver a web message into a real tmux pane a user scrolled into copy mode."""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from omnigent.harnesses.claude_native import bridge

# Stand-in for Claude Code's input box: scrolls a transcript into tmux history,
# frames a composer row, shows a bracketed paste as the draft and commits it on
# the separate submit Enter.
_TERMINAL = r"""
import json
import os
import sys
import tty
from pathlib import Path

delivered = Path(sys.argv[1])
tty.setraw(sys.stdin.fileno())
sys.stdout.write("\x1b[?2004h")
for index in range(40):
    sys.stdout.write(f"transcript line {index}\r\n")

def render(draft=""):
    sys.stdout.write("\x1b[H\x1b[2J" + "─" * 80 + "\r\n❯ " + draft + "\r\n" + "─" * 80 + "\r\n")
    sys.stdout.flush()

render()
buffer = b""
draft = None
messages = []
while True:
    buffer += os.read(sys.stdin.fileno(), 65536)
    if b"\x1b[200~" in buffer and b"\x1b[201~" in buffer:
        pasted, buffer = buffer.split(b"\x1b[200~", 1)[1].split(b"\x1b[201~", 1)
        draft = pasted.decode().strip()
        render(draft)
    if draft is not None and b"\r" in buffer:
        messages.append(draft)
        delivered.write_text(json.dumps(messages))
        draft = None
        buffer = b""
        render()
"""


def _tmux(socket_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["tmux", "-S", str(socket_path), *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )


def _wait_for_composer(socket_path: Path) -> None:
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if bridge._claude_prompt_rendered(bridge._capture_pane(str(socket_path), "main")):
            return
        time.sleep(0.05)
    raise AssertionError("the stand-in terminal never rendered its composer")


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
@pytest.mark.timeout(30)
def test_web_message_delivers_into_a_pane_scrolled_into_copy_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pane left in tmux copy mode (scrolled back) still takes the web message.

    Scrolling back in an attached terminal (Page Up, mouse wheel) puts the
    pane into tmux copy mode, where tmux routes injected keys through the
    copy-mode key table instead of the pane program.
    """
    monkeypatch.setattr(bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path)
    delivered = tmp_path / "delivered.json"
    terminal = tmp_path / "terminal.py"
    terminal.write_text(_TERMINAL)
    with tempfile.TemporaryDirectory(prefix="claude-copy-mode-", dir="/tmp") as socket_dir:
        socket_path = Path(socket_dir) / "tmux.sock"
        command = shlex.join([sys.executable, str(terminal), str(delivered)])
        try:
            launched = _tmux(
                socket_path,
                "-f",
                "/dev/null",
                "new-session",
                "-d",
                "-s",
                "main",
                "-x",
                "80",
                "-y",
                "24",
                command,
            )
            assert launched.returncode == 0, launched.stderr
            _wait_for_composer(socket_path)
            assert int(_tmux(socket_path, "display-message", "-p", "#{history_size}").stdout) > 0

            entered = _tmux(socket_path, "copy-mode", "-t", "main")
            assert entered.returncode == 0, entered.stderr
            assert (
                _tmux(socket_path, "display-message", "-p", "#{pane_in_mode}").stdout.strip()
                == "1"
            )

            bridge_dir = tmp_path / "bridge"
            bridge.write_tmux_target(bridge_dir, socket_path=socket_path, tmux_target="main")
            bridge.inject_user_message(
                bridge_dir, content="hello from the web composer", timeout_s=5.0
            )

            assert delivered.exists(), "the message never reached the pane program"
            assert json.loads(delivered.read_text()) == ["hello from the web composer"]
        finally:
            _tmux(socket_path, "kill-server")
