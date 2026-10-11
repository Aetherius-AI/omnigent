"""Browser e2e: archiving a session must not flash the unread dot on other rows.

The sidebar lights the dot when a row's ``updated_at`` exceeds the viewer's
last-seen baseline from ``GET /v1/sessions``, and an archive makes the server
restate the remaining rows in a ``snapshot`` frame. A WebSocket route makes every
streamed ``updated_at`` lead the list (as the managed deployment's two read paths
do) while ``GET /v1/sessions`` is untouched.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
from playwright.sync_api import Locator, Page, WebSocketRoute, expect

from tests.e2e_ui.conftest import _create_runner_bound_session, _ensure_runner_online

UNSEEN_DOT = '[data-testid="session-state-badge"][data-state="unseen"]'
STREAM_LEAD_S = 2
# Well past the sidebar's watch debounce, the snapshot it triggers, and the list
# refetch that follows.
WATCH_S = 6.0

# Records every change of the set of rows showing an unread dot, so a dot that
# lights and clears between two polls is still observed.
_OBSERVE_UNSEEN_ROWS = """
() => {
  const sel = '[data-testid="session-state-badge"][data-state="unseen"]';
  const rows = () => Array.from(document.querySelectorAll(sel))
    .map((b) => b.closest('li')?.querySelector('a[href^="/c/"]')?.getAttribute('href') ?? '?')
    .sort();
  const log = [];
  let last = JSON.stringify(rows());
  log.push({ t: performance.now(), rows: JSON.parse(last) });
  window.__unseenObserver?.disconnect();
  const observer = new MutationObserver(() => {
    const cur = JSON.stringify(rows());
    if (cur === last) return;
    last = cur;
    log.push({ t: performance.now(), rows: JSON.parse(cur) });
  });
  observer.observe(document.body, {
    subtree: true, childList: true, attributes: true, attributeFilter: ['data-state'],
  });
  window.__unseenObserver = observer;
  window.__unseenRowsLog = log;
}
"""


@pytest.fixture
def seeded_sessions(
    live_server: str,
    runner_id: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, list[str]]]:
    """Four idle, titled, runner-bound sessions; yields ``(base_url, ids)``."""
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    ids: list[str] = []
    try:
        tag = uuid.uuid4().hex[:6]
        for index in range(4):
            ids.append(_create_runner_bound_session(live_server, runner_id))
            httpx.patch(
                f"{live_server}/v1/sessions/{ids[-1]}",
                json={"title": f"e2e-dot-{tag}-{index}"},
                timeout=10.0,
            ).raise_for_status()
        yield live_server, ids
    finally:
        for sid in ids:
            with contextlib.suppress(httpx.HTTPError):
                httpx.delete(f"{live_server}/v1/sessions/{sid}", timeout=10.0)
        if respawned is not None:
            respawned.terminate()
            try:
                respawned.wait(timeout=10)
            except subprocess.TimeoutExpired:
                respawned.kill()
                respawned.wait(timeout=10)


def _row(page: Page, session_id: str) -> Locator:
    return page.locator("li").filter(has=page.locator(f'a[href="/c/{session_id}"]'))


def _archive_from_row(page: Page, session_id: str) -> None:
    row = _row(page, session_id)
    row.hover()
    row.get_by_test_id("conversation-actions").click()
    page.get_by_test_id("archive-conversation").click()


def _frame(payload: str | bytes) -> dict[str, Any] | None:
    text = payload.decode() if isinstance(payload, bytes) else payload
    try:
        frame = json.loads(text)
    except json.JSONDecodeError:
        return None
    return frame if isinstance(frame, dict) else None


def _lead_stream_timestamps(
    ws: WebSocketRoute, lead_s: int, snapshots: list[dict[str, Any]], led: list[str]
) -> None:
    """Forward the stream, adding ``lead_s`` to every streamed ``updated_at``.

    Collects the ``snapshot`` frames and the ids whose timestamp was shifted, so
    the test can wait for snapshots and prove the split was exercised.
    """
    server = ws.connect_to_server()
    ws.on_message(server.send)

    def from_server(message: str | bytes) -> None:
        frame = _frame(message)
        if frame is None or frame.get("type") not in ("snapshot", "changed"):
            ws.send(message)
            return
        for item in frame.get("items", []):
            if isinstance(item.get("updated_at"), int):
                item["updated_at"] += lead_s
                led.append(item["id"])
        if frame["type"] == "snapshot":
            snapshots.append(frame)
        ws.send(json.dumps(frame))

    server.on_message(from_server)


def _wait_until(predicate: Callable[[], bool], page: Page, timeout_s: float, what: str) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        page.wait_for_timeout(100)


def _wait_for_snapshot_after(snapshots: list[dict[str, Any]], seen: int, page: Page) -> None:
    _wait_until(lambda: len(snapshots) > seen, page, 10.0, "the snapshot after archiving")


def _observe_dots(page: Page, seconds: float) -> list[dict[str, Any]]:
    """Keep the page alive for ``seconds`` and return the observer's row-set log."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        page.wait_for_timeout(100)
    return page.evaluate("() => window.__unseenRowsLog")


def _other_rows_lit(log: list[dict[str, Any]], t_action_ms: float, exclude: str) -> dict[str, Any]:
    """Which rows other than ``exclude`` showed a dot after the action, and when."""
    lit: dict[str, float] = {}
    for entry in log:
        if entry["t"] < t_action_ms:
            continue
        for href in entry["rows"]:
            if href != exclude and href not in lit:
                lit[href] = round((entry["t"] - t_action_ms) / 1000, 3)
    return lit


def _drive_archive_journey(
    page: Page, base_url: str, ids: list[str], snapshots: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Open the sidebar with every row read, then archive two non-active rows.

    Returns, per archived session, the other rows that lit the dot and when.
    """
    page.goto(f"{base_url}/c/{ids[0]}")
    for sid in ids:
        expect(_row(page, sid)).to_be_visible()
    _wait_until(lambda: len(snapshots) > 0, page, 20.0, "the session-updates snapshot")
    page.evaluate(_OBSERVE_UNSEEN_ROWS)
    _observe_dots(page, 3.0)
    expect(page.locator(UNSEEN_DOT)).to_have_count(0)

    lit_after_archive: dict[str, dict[str, Any]] = {}
    for target in ids[-1:-3:-1]:
        page.evaluate(_OBSERVE_UNSEEN_ROWS)
        seen = len(snapshots)
        t_click_ms = page.evaluate("() => performance.now()")
        _archive_from_row(page, target)
        expect(page.locator(f'a[href="/c/{target}"]')).to_have_count(0)
        _wait_for_snapshot_after(snapshots, seen, page)
        log = _observe_dots(page, WATCH_S)
        lit_after_archive[target] = _other_rows_lit(log, t_click_ms, f"/c/{target}")
    return lit_after_archive


def test_archiving_leaves_other_rows_dots_off_when_stream_timestamps_lead_the_list(
    request: pytest.FixtureRequest,
    seeded_sessions: tuple[str, list[str]],
) -> None:
    base_url, ids = seeded_sessions
    page: Page = request.getfixturevalue("page")
    snapshots: list[dict[str, Any]] = []
    led: list[str] = []
    page.route_web_socket(
        "**/v1/sessions/updates*",
        lambda ws: _lead_stream_timestamps(ws, STREAM_LEAD_S, snapshots, led),
    )

    lit = _drive_archive_journey(page, base_url, ids, snapshots)

    assert led, "no streamed updated_at was shifted, so the stream/list split was not exercised"
    assert all(rows == {} for rows in lit.values()), (
        f"unread dot lit on other rows after archiving: {json.dumps(lit, indent=1)}"
    )
