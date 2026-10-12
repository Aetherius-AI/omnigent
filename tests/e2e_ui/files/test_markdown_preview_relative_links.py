"""E2E: relative links in a Markdown file open the linked workspace file in the viewer,
instead of the browser resolving ``[plan](plan.md)`` against ``/c/<session id>`` into a
bogus session. Covers Preview clicks and Edit-mode Ctrl/Cmd-clicks."""

from __future__ import annotations

import re
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Locator, Page, Response, expect

from tests.e2e_ui.conftest import open_right_rail, switch_markdown_view_mode

_README_PATH = "notes/README.md"
_PLAN_BODY = "PLAN-MARKER body of the linked plan."
_FILES = {
    _README_PATH: (
        "# Notes\n\n"
        "See the [plan](plan.md), the [refs](references/) folder and the\n"
        "[diagram](../docs/architecture.mmd).\n\n"
        "External: [Omnigent docs](https://omnigent.ai/docs). "
        "In-page: [Jump to details](#details).\n\n"
        "## Details\n\nBody text.\n"
    ),
    "notes/plan.md": f"# Plan\n\n{_PLAN_BODY}\n",
    "notes/references/index.md": "# References\n",
    "docs/architecture.mmd": "flowchart LR\n  A --> B\n",
}


@pytest.fixture
def seeded_relative_links_session(
    seeded_session: tuple[str, str],
) -> Iterator[tuple[str, str]]:
    """Seed ``notes/README.md`` with relative links and the files it links to."""
    base_url, session_id = seeded_session
    filesystem = f"{base_url}/v1/sessions/{session_id}/resources/environments/default/filesystem"
    for path, content in _FILES.items():
        resp = httpx.put(
            f"{filesystem}/{path}", json={"content": content, "encoding": "utf-8"}, timeout=10.0
        )
        resp.raise_for_status()
    yield (base_url, session_id)


def _session_url(session_id: str) -> re.Pattern[str]:
    return re.compile(rf"/c/{re.escape(session_id)}(?:[?#]|$)")


def _open_readme(page: Page, base_url: str, session_id: str, mode: str) -> Locator:
    """Open ``notes/README.md`` from the Files tree in ``mode`` and return the viewer locator."""
    page.goto(f"{base_url}/c/{session_id}")
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name=re.compile(r"^Files")).click()
    folder = rail.get_by_role("button", name=re.compile(r"^notes\b")).first
    expect(folder).to_be_visible(timeout=30_000)
    if folder.get_attribute("aria-expanded") != "true":
        folder.click()
    rail.get_by_role("button", name=re.compile(r"^README\.md\b")).first.click()
    file_viewer = rail.get_by_test_id("file-viewer")
    expect(file_viewer).to_be_visible()
    expect(page.get_by_role("button", name="Close README.md", exact=True).first).to_be_visible()
    switch_markdown_view_mode(page, file_viewer, mode)
    surface = ".markdown-preview" if mode == "Preview" else "[contenteditable='true']"
    expect(file_viewer.locator(surface)).to_be_visible()
    return file_viewer


def test_preview_relative_file_link_opens_the_linked_file(
    request: pytest.FixtureRequest,
    seeded_relative_links_session: tuple[str, str],
) -> None:
    """Clicking ``[plan](plan.md)`` in Preview opens ``plan.md`` in the viewer."""
    base_url, session_id = seeded_relative_links_session
    page: Page = request.getfixturevalue("page")
    try:
        file_viewer = _open_readme(page, base_url, session_id, "Preview")
        preview = file_viewer.locator(".markdown-preview")
        link = preview.get_by_text("plan", exact=True)
        expect(link).to_be_visible()
        link.click()

        plan_tab = page.get_by_role("button", name="Close plan.md", exact=True).first
        try:
            expect(plan_tab).to_be_visible()
        except AssertionError as exc:
            raise AssertionError(
                f"the linked file did not open in the file viewer; the document is at {page.url}"
            ) from exc
        expect(page).to_have_url(_session_url(session_id))
        expect(file_viewer.get_by_text(_PLAN_BODY).first).to_be_visible()
    finally:
        page.context.close()


def test_preview_relative_folder_link_stays_in_the_session(
    request: pytest.FixtureRequest,
    seeded_relative_links_session: tuple[str, str],
) -> None:
    """Clicking ``[refs](references/)`` must not load a session named ``references``."""
    base_url, session_id = seeded_relative_links_session
    page: Page = request.getfixturevalue("page")
    stray_session_requests: list[str] = []

    def track(response: Response) -> None:
        if "/v1/sessions/references" in response.url:
            stray_session_requests.append(f"{response.status} {response.url}")

    page.on("response", track)
    try:
        file_viewer = _open_readme(page, base_url, session_id, "Preview")
        link = file_viewer.locator(".markdown-preview").get_by_text("refs", exact=True)
        expect(link).to_be_visible()
        link.click()
        # Observation window: a bogus session keeps reconnecting its stream.
        page.wait_for_timeout(6_000)
        assert _session_url(session_id).search(page.url), f"left the session for {page.url}"
        assert not stray_session_requests, (
            "requests were made for a session named after the folder: "
            f"{stray_session_requests[:5]}"
        )
    finally:
        page.context.close()


def test_editor_ctrl_click_relative_link_does_not_open_a_bogus_session_tab(
    request: pytest.FixtureRequest,
    seeded_relative_links_session: tuple[str, str],
) -> None:
    """Ctrl-clicking ``[plan](plan.md)`` in Edit mode opens ``plan.md`` in the viewer."""
    base_url, session_id = seeded_relative_links_session
    page: Page = request.getfixturevalue("page")
    opened: list[Page] = []
    page.context.on("page", lambda popup: opened.append(popup))
    try:
        file_viewer = _open_readme(page, base_url, session_id, "Edit")
        editor = file_viewer.locator("[contenteditable='true']")
        expect(editor).to_be_visible()
        anchor = editor.locator("a", has_text="plan").first
        expect(anchor).to_have_attribute("href", "plan.md")
        anchor.click(modifiers=["Control"])
        page.wait_for_timeout(3_000)

        for popup in opened:
            popup.wait_for_load_state()
        bogus_tabs = [popup.url for popup in opened if "/c/plan.md" in popup.url]
        assert not bogus_tabs, f"a new tab opened on a session named after the file: {bogus_tabs}"
        expect(page).to_have_url(_session_url(session_id))
        expect(page.get_by_role("button", name="Close plan.md", exact=True).first).to_be_visible()
        expect(file_viewer.get_by_text(_PLAN_BODY).first).to_be_visible()
    finally:
        page.context.close()
