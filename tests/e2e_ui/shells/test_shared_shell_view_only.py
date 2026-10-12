"""E2E: a non-owner (Edit or Read) attaches to a shared session's shell read-only,
and the terminal says so with a "View only" badge instead of dropping keystrokes."""

from __future__ import annotations

import re
import uuid

import httpx
import pytest
from playwright.sync_api import Browser, Locator, Page, expect

from tests.e2e_ui.conftest import open_right_rail

# Mirrors omnigent/server/auth.py.
_LEVEL_READ = 1
_LEVEL_EDIT = 2

_SHELL_TAB = re.compile(r"^zsh · u-")


def _grant(base_url: str, session_id: str, email: str, level: int) -> None:
    """Grant *email* *level* on the session as the headerless ``local`` owner."""
    httpx.put(
        f"{base_url}/v1/sessions/{session_id}/permissions",
        json={"user_id": email, "level": level},
        timeout=10.0,
    ).raise_for_status()


def _workspace_rail(page: Page) -> Locator:
    return page.get_by_role("complementary", name="Workspace")


def _connected_terminal(rail: Locator) -> Locator:
    terminal_view = rail.get_by_test_id("terminal-view").last
    expect(terminal_view).to_be_visible(timeout=60_000)
    expect(terminal_view).to_have_attribute("data-state", "connected", timeout=20_000)
    return terminal_view


def _owner_opens_shell(page: Page, base_url: str, session_id: str) -> Locator:
    """Open the session as the owner, launch a shell from the rail, return its xterm."""
    page.goto(f"{base_url}/c/{session_id}")
    open_right_rail(page)
    rail = _workspace_rail(page)
    rail.get_by_role("button", name="Open new").click()
    page.get_by_role("menuitem", name=re.compile("Shell")).click()
    expect(rail.get_by_role("button", name=_SHELL_TAB)).to_be_visible(timeout=60_000)
    return _connected_terminal(rail)


def _collaborator_opens_shell(page: Page, base_url: str, session_id: str) -> Locator:
    """Open the shared session as a collaborator and select the owner's shell tab."""
    page.goto(f"{base_url}/c/{session_id}")
    open_right_rail(page)
    rail = _workspace_rail(page)
    tab = rail.get_by_role("button", name=_SHELL_TAB)
    expect(tab).to_be_visible(timeout=60_000)
    tab.click()
    return _connected_terminal(rail)


def _attach_socket_urls(page: Page) -> list[str]:
    urls: list[str] = []
    page.on("websocket", lambda ws: urls.append(ws.url) if "/attach" in ws.url else None)
    return urls


@pytest.mark.parametrize(
    ("level", "label"),
    [
        pytest.param(_LEVEL_EDIT, "edit", id="edit-collaborator"),
        pytest.param(_LEVEL_READ, "read", id="read-viewer"),
    ],
)
def test_non_owner_sees_view_only_badge_on_shared_shell(
    browser: Browser,
    terminal_session: tuple[str, str],
    level: int,
    label: str,
) -> None:
    base_url, session_id = terminal_session
    collaborator = f"{label}-{uuid.uuid4().hex[:8]}@ui.test"

    owner_ctx = browser.new_context()
    try:
        owner_terminal = _owner_opens_shell(owner_ctx.new_page(), base_url, session_id)
        # The owner types for real, so no badge is shown to them.
        expect(owner_terminal.get_by_test_id("terminal-read-only-badge")).to_have_count(0)
        _grant(base_url, session_id, collaborator, level)

        collaborator_ctx = browser.new_context(
            extra_http_headers={"X-Forwarded-Email": collaborator}
        )
        try:
            page = collaborator_ctx.new_page()
            attach_urls = _attach_socket_urls(page)
            terminal = _collaborator_opens_shell(page, base_url, session_id)

            badge = terminal.get_by_test_id("terminal-read-only-badge")
            expect(badge).to_be_visible(timeout=5_000)
            expect(badge).to_have_text("View only")
            assert attach_urls and all("read_only=true" in url for url in attach_urls), attach_urls
            if page.video is not None:
                # Hold the badge on screen long enough to read in the recording.
                page.wait_for_timeout(2_000)
                print(f"{label} collaborator video: {page.video.path()}")
        finally:
            collaborator_ctx.close()
    finally:
        owner_ctx.close()
