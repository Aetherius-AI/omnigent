"""Web-composer delivery while the Claude pane is scrolled into tmux copy mode.

Journey: a Claude-native session answers a first web message; from a shell on
the runner host (opened from the session's workspace rail) the user attaches to
the same terminal with the production ``tmux … attach`` command and scrolls back
(Page Up, mouse wheel), which puts the pane into tmux copy mode; the next
web-composer message must still be delivered and answered. When Claude Code
holds the alternate screen the gesture passes through to it, so the test falls
back to tmux's own ``copy-mode`` command and logs which route it took.

Runs against the real Claude Code CLI on the mock model. Pane state is read on
the runner host and written to a file under a directory both the runner and
this test can see (``OMNIGENT_E2E_SHARED_DIR``, default ``tmp_path``).
"""

from __future__ import annotations

import logging
import os
import re
import time
import uuid
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

from tests.e2e_ui.conftest import open_right_rail, reset_mock_llm

from .test_message_render_parity import _ensure_chat_view
from .test_native_claude_render_parity import (
    _open_terminal_view,
    _send_composer_turn,
    _wait_terminal_connected,
)

_log = logging.getLogger(__name__)

_ERROR_PILL = '[data-testid="error-pill"]'
_XTERM_INPUT = "textarea.xterm-helper-textarea"
_SHELL_READY_TIMEOUT_MS = 60_000
_PANE_STATE_TIMEOUT_S = 20.0
_COPY_MODE_TIMEOUT_S = 4.0
_COPY_MODE_ATTEMPTS = 3
_SHARED_DIR_ENV = "OMNIGENT_E2E_SHARED_DIR"


def _claude_terminal_tmux(base_url: str, session_id: str) -> tuple[str, str]:
    """Return the Claude terminal's tmux socket and target from its resource.

    The same metadata ``omnigent claude`` reads for its direct local attach.

    :param base_url: Server base URL.
    :param session_id: The session/conversation id.
    :returns: ``(tmux_socket, tmux_target)``.
    """
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}/resources", timeout=10.0)
    resp.raise_for_status()
    for resource in resp.json().get("data", []):
        metadata = resource.get("metadata") or {}
        if resource.get("type") == "terminal" and metadata.get("tmux_socket"):
            return str(metadata["tmux_socket"]), str(metadata.get("tmux_target") or "main")
    raise AssertionError(f"no terminal resource with tmux metadata: {resp.json()}")


def _open_new_shell(page: Page) -> Locator:
    """Open a shell from the workspace rail's "+" menu and wait for it to connect.

    :param page: Playwright page on the session route.
    :returns: The new shell's terminal view.
    """
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("button", name="Open new").click()
    page.get_by_role("menuitem", name=re.compile("Shell")).click()
    terminal_view = rail.get_by_test_id("terminal-view").last
    expect(terminal_view).to_be_visible(timeout=_SHELL_READY_TIMEOUT_MS)
    expect(terminal_view).to_have_attribute(
        "data-state", "connected", timeout=_SHELL_READY_TIMEOUT_MS
    )
    return terminal_view


def _type_shell_command(page: Page, terminal_view: Locator, command: str) -> None:
    """Type one command line into a connected shell terminal and submit it."""
    terminal_view.locator(_XTERM_INPUT).focus()
    page.keyboard.type(command, delay=5)
    page.keyboard.press("Enter")


def _pane_state(state_file: Path) -> dict[str, str]:
    """Parse the pane-state file written on the runner host."""
    try:
        fields = state_file.read_text().split()
    except OSError:
        return {}
    keys = ("pane_in_mode", "history_size", "alternate_on", "clients")
    return dict(zip(keys, fields, strict=False))


def _wait_pane_state(
    page: Page, state_file: Path, timeout_s: float, **expected: str
) -> dict[str, str]:
    """Poll the pane-state file until every *expected* field matches or time runs out."""
    deadline = time.monotonic() + timeout_s
    state: dict[str, str] = {}
    while time.monotonic() < deadline:
        state = _pane_state(state_file)
        if state and all(state.get(key) == value for key, value in expected.items()):
            return state
        page.wait_for_timeout(250)
    return state


def _attach_from_runner_shell(
    page: Page, shell: Locator, *, socket: str, target: str, state_file: Path
) -> dict[str, str]:
    """Start the pane-state writer, then attach the shell to the Claude terminal.

    Mirrors ``omnigent.harnesses.claude_native.main._attach_direct_tmux``:
    ``tmux -S <socket> -f /dev/null attach -t <target>`` with ``TMUX`` unset.

    The state writer runs until ``<state_file>.stop`` exists.

    :returns: The pane state once the attach shows up as one more client.
    """
    fmt = "#{pane_in_mode} #{history_size} #{alternate_on} #{session_attached}"
    # tmux errors go to a sibling file so they cannot be parsed as pane fields.
    err_file = state_file.with_suffix(".err")
    stop_file = state_file.with_suffix(".stop")
    monitor = (
        f"( while [ ! -e '{stop_file}' ]; do tmux -S '{socket}' display-message -p "
        f"-t '{target}' '{fmt}' > '{state_file}.tmp' 2> '{err_file}'; "
        f"mv -f '{state_file}.tmp' '{state_file}'; sleep 0.3; done ) > /dev/null 2>&1 &"
    )
    _type_shell_command(page, shell, monitor)
    before = _wait_pane_state(page, state_file, _PANE_STATE_TIMEOUT_S)
    tmux_error = err_file.read_text().strip() if err_file.exists() else ""
    assert before, (
        f"pane state never arrived from the runner shell (tmux stderr={tmux_error!r}); is "
        f"{state_file.parent} shared with the runner host?"
    )
    assert before.get("pane_in_mode") == "0", f"pane was already in a tmux mode: {before!r}"
    attached_clients = str(int(before.get("clients", "0")) + 1)
    _type_shell_command(
        page,
        shell,
        f"env -u TMUX TERM=xterm-256color tmux -S '{socket}' -f /dev/null attach -t '{target}'",
    )
    state = _wait_pane_state(page, state_file, _PANE_STATE_TIMEOUT_S, clients=attached_clients)
    assert state.get("clients") == attached_clients, (
        f"the native tmux attach never connected (before {before!r}, now {state!r})"
    )
    # Give the nested client a moment to settle before sending it keys.
    page.wait_for_timeout(1_500)
    return state


def _scroll_back_into_copy_mode(
    page: Page, shell: Locator, state_file: Path
) -> tuple[str, dict[str, str]]:
    """Scroll the attached shell back (Page Up, then the mouse wheel) into copy mode.

    :returns: ``(route, state)`` — how copy mode was reached and the pane state.
    """
    for _ in range(_COPY_MODE_ATTEMPTS):
        shell.locator(_XTERM_INPUT).focus()
        page.keyboard.press("PageUp")
        state = _wait_pane_state(page, state_file, _COPY_MODE_TIMEOUT_S, pane_in_mode="1")
        if state.get("pane_in_mode") == "1":
            return "Page Up in a native tmux attach from a runner-host shell", state
    box = shell.locator(".xterm-screen").first.bounding_box()
    if box is not None:
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        for _ in range(10):
            page.mouse.wheel(0, -40)
            page.wait_for_timeout(50)
        state = _wait_pane_state(page, state_file, _COPY_MODE_TIMEOUT_S, pane_in_mode="1")
        if state.get("pane_in_mode") == "1":
            return "mouse wheel in a native tmux attach from a runner-host shell", state
    return "neither Page Up nor the mouse wheel entered copy mode", _pane_state(state_file)


def _force_copy_mode(page: Page, *, socket: str, target: str, state_file: Path) -> dict[str, str]:
    """Fallback from the ticket: enter copy mode with the tmux command from a second shell."""
    second = _open_new_shell(page)
    _type_shell_command(page, second, f"tmux -S '{socket}' copy-mode -t '{target}'")
    return _wait_pane_state(page, state_file, _COPY_MODE_TIMEOUT_S, pane_in_mode="1")


@pytest.mark.nightly
@pytest.mark.timeout(600)
def test_native_claude_composer_delivers_while_pane_in_copy_mode(
    request: pytest.FixtureRequest,
    native_claude_mock_session: tuple[str, str],
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """A web-composer message lands while the terminal pane sits in copy mode."""
    base_url, session_id = native_claude_mock_session
    _log.info("copy-mode journey: base_url=%s session_id=%s", base_url, session_id)
    shared_dir = Path(os.environ.get(_SHARED_DIR_ENV) or tmp_path)
    shared_dir.mkdir(parents=True, exist_ok=True)
    state_file = shared_dir / f"pane-state-{session_id}.txt"
    # Create the recorded page only after the non-browser setup above.
    page: Page = request.getfixturevalue("page")

    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _ensure_chat_view(page)
    reset_mock_llm(mock_llm_server_url)

    nonce = uuid.uuid4().hex[:8]
    _send_composer_turn(
        page,
        mock_llm_server_url,
        base_url=base_url,
        session_id=session_id,
        index=1,
        user_marker=f"usr-1-{nonce}",
        assistant_token=f"ast-1-{nonce}",
        expected_user_bubbles=1,
    )
    socket, target = _claude_terminal_tmux(base_url, session_id)
    _log.info("baseline turn answered; Claude terminal tmux target=%s", target)

    shell = _open_new_shell(page)
    attached = _attach_from_runner_shell(
        page, shell, socket=socket, target=target, state_file=state_file
    )
    request.addfinalizer(state_file.with_suffix(".stop").touch)
    _log.info("native attach from the runner shell is up; pane state %s", attached)
    route, state = _scroll_back_into_copy_mode(page, shell, state_file)
    if state.get("pane_in_mode") != "1":
        _log.warning("%s (pane state %s); using the ticket's tmux copy-mode step", route, state)
        state = _force_copy_mode(page, socket=socket, target=target, state_file=state_file)
        route = f"tmux copy-mode command from a second runner shell ({route}: {state})"
    assert state.get("pane_in_mode") == "1", f"pane never entered copy mode: {state!r}"
    _log.info(
        "pane is in copy mode via %s (state %s); sending from the web composer", route, state
    )

    _ensure_chat_view(page)
    _send_composer_turn(
        page,
        mock_llm_server_url,
        base_url=base_url,
        session_id=session_id,
        index=2,
        user_marker=f"usr-2-{nonce}",
        assistant_token=f"ast-2-{nonce}",
        expected_user_bubbles=2,
    )
    state_after = _pane_state(state_file)
    _log.info("copy-mode send answered (%s); pane state after=%s", route, state_after)
    expect(page.locator(_ERROR_PILL)).to_have_count(0)
    assert state_after.get("pane_in_mode") == "0", f"pane still in a tmux mode: {state_after!r}"
