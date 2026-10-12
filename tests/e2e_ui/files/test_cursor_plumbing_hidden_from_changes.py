"""E2E: Omnigent's injected Cursor plumbing must not show as session changes.

Binding a cursor-native session makes the runner write ``.cursor/mcp.json`` and
``.cursor/hooks.json`` into the workspace before the Cursor TUI launches. In a
git workspace those untracked files reached the Workspace rail's Changes tab as
Added although the user changed nothing. This drives the real journey (clean
git project, runner-bound cursor-native session, Changes tab) and asserts the
correct behavior, so it fails on a build with the bug. Needs ``cursor-agent``
and ``tmux`` on PATH; the plumbing is written before Cursor authenticates, so
no Cursor login is required.
"""

from __future__ import annotations

import contextlib
import re
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _REPO_ROOT,
    _create_native_cursor_session,
    _ensure_runner_online,
    _server_state,
    open_right_rail,
)

# Plumbing the runner injects under <workspace>/.cursor/ when it auto-creates
# the Cursor TUI on bind. The SDK harness's omnigent-hook.sh is not written on
# this path; the registry unit tests cover it.
_INJECTED = ("mcp.json", "hooks.json")

# cursor auto-launch + rail hydration.
_RAIL_READY_TIMEOUT_MS = 120_000
# How long the runner may take to inject the plumbing after bind.
_INJECT_TIMEOUT_S = 150.0


def _cursor_terminal_unavailable() -> str | None:
    """Return a skip reason when the cursor auto-terminal cannot run here."""
    if shutil.which("cursor-agent") is None:
        return "needs the `cursor-agent` binary on PATH (runner auto-creates the Cursor TUI)."
    if shutil.which("tmux") is None:
        return "needs `tmux` on PATH (runner-owned Cursor TUI pane)."
    return None


@pytest.fixture
def cursor_git_workspace_session(
    live_server: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str, Path]]:
    """A runner-bound cursor-native session pinned to a fresh, clean git repo.

    The workspace lives under the repo tree (gitignored) so an out-of-process
    runner shares this test's view of it; binding writes the ``.cursor`` plumbing.

    :returns: ``(base_url, session_id, workspace)``.
    """
    reason = _cursor_terminal_unavailable()
    if reason:
        pytest.skip(reason)

    ws_root = _REPO_ROOT / ".omnigent" / "e2e-tmp"
    ws_root.mkdir(parents=True, exist_ok=True)
    workspace = ws_root / f"cursor-plumbing-{uuid.uuid4().hex[:8]}"
    workspace.mkdir()

    def _git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=str(workspace), check=True, capture_output=True)

    respawned: subprocess.Popen[bytes] | None = None
    session_id: str | None = None
    try:
        _git("init", "-q")
        _git("config", "user.email", "e2e@example.com")
        _git("config", "user.name", "e2e")
        (workspace / "README.md").write_text("# project\n", encoding="utf-8")
        _git("add", "-A")
        _git("commit", "-qm", "init")

        respawned = _ensure_runner_online(live_server, tmp_path_factory)
        runner_id = str(_server_state["runner_id"])
        session_id = _create_native_cursor_session(live_server, runner_id, workspace=workspace)
        yield (live_server, session_id, workspace)
    finally:
        if session_id is not None:
            with contextlib.suppress(httpx.HTTPError):
                httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        shutil.rmtree(workspace, ignore_errors=True)
        if respawned is not None:
            respawned.terminate()
            try:
                respawned.wait(timeout=5)
            except subprocess.TimeoutExpired:
                respawned.kill()
                respawned.wait(timeout=5)


@pytest.mark.nightly
@pytest.mark.timeout(600)
def test_cursor_plumbing_hidden_from_session_changes(
    request: pytest.FixtureRequest,
    cursor_git_workspace_session: tuple[str, str, Path],
) -> None:
    """Injected .cursor/mcp.json + hooks.json must not appear as session changes."""
    base_url, session_id, workspace = cursor_git_workspace_session
    cursor_dir = workspace / ".cursor"

    # Wait for both injected files before checking that Changes hides them.
    deadline = time.monotonic() + _INJECT_TIMEOUT_S
    while not all((cursor_dir / n).exists() for n in _INJECTED) and time.monotonic() < deadline:
        time.sleep(1)
    for n in _INJECTED:
        assert (cursor_dir / n).exists(), (
            f"runner never injected .cursor/{n}; the journey did not reach the "
            "state this test guards."
        )

    # Open the browser only now, so a recording starts at the user's journey
    # rather than at the runner bind that precedes it.
    page: Page = request.getfixturevalue("page")
    page.goto(f"{base_url}/c/{session_id}")

    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    changes_tab = rail.get_by_role("tab", name=re.compile("^Changes"))
    expect(changes_tab).to_be_visible(timeout=_RAIL_READY_TIMEOUT_MS)
    changes_tab.click()
    expect(changes_tab).to_have_attribute("aria-selected", "true")

    # Correct behavior. On a build with the bug the rail instead lists the two
    # injected files under a "Changes 2 changed" badge, so each assertion here
    # is what fails.
    expect(rail.get_by_text("No workspace changes yet")).to_be_visible(timeout=30_000)
    for n in _INJECTED:
        expect(rail.get_by_text(n, exact=True)).to_have_count(0)

    # The changed-files endpoint the panel reads must also omit the plumbing.
    resp = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/resources/environments/default/changes",
        timeout=30.0,
    )
    resp.raise_for_status()
    paths = [entry["path"] for entry in resp.json().get("data", [])]
    injected = [p for p in paths if p.endswith((".cursor/mcp.json", ".cursor/hooks.json"))]
    assert not injected, f"changes endpoint listed Omnigent's injected plumbing: {injected}"
