"""E2E: a session search that outlives the client's deadline reads as a timeout.

The browser holds the ``GET /v1/sessions?search_query=`` request open so only the
client's ``SEARCH_FETCH_TIMEOUT_MS`` deadline can end it; the palette must then say
the search timed out rather than the generic "Couldn't load sessions.".
"""

from __future__ import annotations

import contextlib
import re
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from playwright.sync_api import Error, Page, Route, expect

# Mirrors SEARCH_FETCH_TIMEOUT_MS in web/src/hooks/useConversations.ts.
SEARCH_FETCH_TIMEOUT_MS = 10_000
_SEARCH_URL = re.compile(r"/v1/sessions\?.*search_query=")


def _hold_search_requests(page: Page, term: str) -> tuple[list[float], list[Route]]:
    """Leave every search for ``term`` unanswered; return their send times and routes."""
    sent: list[float] = []
    held: list[Route] = []

    def _handle(route: Route) -> None:
        query = parse_qs(urlparse(route.request.url).query)
        if query.get("search_query") != [term]:
            route.continue_()
            return
        sent.append(time.monotonic())
        held.append(route)

    page.route(_SEARCH_URL, _handle)
    return sent, held


def test_search_past_client_deadline_reports_timeout(
    request: pytest.FixtureRequest,
    seeded_session: tuple[str, str],
    tmp_path: Path,
) -> None:
    base_url, session_id = seeded_session
    httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"title": f"e2e-search-timeout-{uuid.uuid4().hex[:8]}"},
        timeout=10.0,
    ).raise_for_status()
    term = f"zzz-no-such-session-{uuid.uuid4().hex[:8]}"

    page: Page = request.getfixturevalue("page")
    sent, held = _hold_search_requests(page, term)

    try:
        page.goto(f"{base_url}/c/{session_id}")
        search_button = page.get_by_test_id("sidebar-search-button")
        expect(search_button).to_be_visible(timeout=30_000)
        search_button.click()

        dialog = page.get_by_role("dialog")
        palette_input = page.get_by_test_id("command-palette-input")
        expect(palette_input).to_be_visible()
        palette_input.fill(term)
        expect(dialog.get_by_text("Searching…")).to_be_visible()

        status = dialog.get_by_role("status")
        expect(status).to_be_visible(timeout=SEARCH_FETCH_TIMEOUT_MS + 5_000)
        elapsed_s = time.monotonic() - sent[0]
        page.screenshot(path=str(tmp_path / "palette-after-deadline.png"))
        # Only the client deadline could have ended the held request.
        assert elapsed_s >= SEARCH_FETCH_TIMEOUT_MS / 1000 - 0.5, elapsed_s
        expect(dialog.get_by_role("button", name="Retry")).to_be_visible()

        expect(status).to_contain_text(re.compile("timed out", re.IGNORECASE))
        expect(status).not_to_contain_text("Couldn't load sessions.")
    finally:
        # The browser already gave up on the held search; settle its route so closing stays quiet.
        for route in held:
            with contextlib.suppress(Error):
                route.abort()
