"""Real browser/server/runner/tmux journeys with lost or delayed fake Kiro input."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from omnigent.harnesses.kiro_native.bridge import acp_record_path
from omnigent.harnesses.kiro_native.permissions import kiro_permission_elicitation_id

from .test_kiro_concurrent_permissions import (
    _FAKE_KIRO_SOURCE,
    _pending_elicitations,
    _recorder_response_ids,
    _wait_for,
)
from .test_kiro_concurrent_permissions import (
    kiro_shim_session as _kiro_shim_session,
)
from .test_message_render_parity import _ensure_chat_view, _send

kiro_shim_session = _kiro_shim_session


def _shim(mode: str) -> str:
    source = _FAKE_KIRO_SOURCE.replace(
        'COMMANDS = ("alpha", "beta", "gamma")', 'COMMANDS = ("alpha", "alpha")'
    )
    return source.replace(
        "    def on_enter(self):\n        if self.active is not None:",
        f"""    def on_enter(self):
        if self.active is not None:
            with open(RECORD_PATH + ".enters", "a") as handle:
                handle.write(self.active["id"] + "\\n")
            if not getattr(self, "first_enter_seen", False):
                self.first_enter_seen = True
                if {mode!r} == "drop":
                    return
                import time
                time.sleep(9)
""",
    )


@pytest.mark.timeout(180)
@pytest.mark.parametrize("kiro_shim_session", [_shim("drop")], indirect=True, ids=["lost-enter"])
@pytest.mark.parametrize(
    "surface", ["chat", "mobile", "inbox", "standalone", "reject", "terminal"]
)
def test_kiro_manual_retry(
    kiro_shim_session: tuple[str, str, Path], page: Page, surface: str, tmp_path: Path
) -> None:
    base_url, session_id, bridge_dir = kiro_shim_session
    record_file = acp_record_path(bridge_dir)
    first_id = "perm-req-1-1"
    eid = kiro_permission_elicitation_id(session_id, first_id)
    if surface == "mobile":
        page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{base_url}/c/{session_id}")
    _ensure_chat_view(page)
    _send(page, "Run two identical commands requiring separate approvals.")
    card = page.locator('[data-testid="approval-card"][data-state="pending"]').first
    expect(card).to_be_visible(timeout=120_000)
    if surface == "inbox":
        page.goto(f"{base_url}/inbox")
    elif surface == "standalone":
        page.goto(f"{base_url}/approve/{session_id}/{eid}")
    started = time.monotonic()
    page.get_by_role(
        "button", name="Reject" if surface == "reject" else "Approve", exact=True
    ).click()
    expect(page.get_by_text("Waiting for Kiro to confirm")).to_be_visible()
    retry = page.get_by_role(
        "button",
        name="Retry sending rejection" if surface == "reject" else "Retry sending approval",
    )
    expect(retry).not_to_be_visible()
    expect(retry).to_be_visible(timeout=12_000)
    assert time.monotonic() - started >= 5.0
    assert _recorder_response_ids(record_file) == set()
    assert record_file.with_suffix(record_file.suffix + ".enters").read_text().splitlines() == [
        first_id
    ]
    page.reload()
    expect(retry).to_be_visible(timeout=15_000)
    expect(
        page.get_by_text("The previous attempt may still be pending.", exact=False)
    ).to_be_visible()
    terminal = page.get_by_role("link", name="Open terminal")
    expect(terminal).to_have_attribute("href", f"/c/{session_id}?view=terminal")
    page.screenshot(path=str(tmp_path / "delivery-unconfirmed.png"), full_page=True)
    if surface == "terminal":
        terminal.click()
        terminal_view = page.get_by_test_id("terminal-view").last
        expect(terminal_view).to_have_attribute("data-state", "connected", timeout=30_000)
        terminal_view.locator(".xterm-helper-textarea").focus()
        page.keyboard.press("Enter")
        page.goto(f"{base_url}/c/{session_id}?view=chat")
    else:
        retry.click()
    _wait_for(lambda: first_id in _recorder_response_ids(record_file), timeout_s=15)
    expect(retry).not_to_be_visible(timeout=15_000)
    second_eid = kiro_permission_elicitation_id(session_id, "perm-req-1-2")
    _wait_for(
        lambda: second_eid in json.dumps(_pending_elicitations(base_url, session_id)), timeout_s=15
    )
    page.wait_for_timeout(1_000)
    assert _recorder_response_ids(record_file) == {first_id}
    assert record_file.with_suffix(record_file.suffix + ".enters").read_text().splitlines() == [
        first_id,
        first_id,
    ]
    responses = [json.loads(line)["msg"] for line in record_file.read_text().splitlines()]
    result = next(
        msg["result"] for msg in responses if msg.get("id") == first_id and "result" in msg
    )
    assert result["outcome"]["optionId"] == ("reject-1-1" if surface == "reject" else "allow-1-1")


@pytest.mark.timeout(180)
@pytest.mark.parametrize("kiro_shim_session", [_shim("delay")], indirect=True, ids=["late-enter"])
def test_kiro_late_confirmation_removes_retry(
    kiro_shim_session: tuple[str, str, Path], page: Page
) -> None:
    base_url, session_id, bridge_dir = kiro_shim_session
    record_file = acp_record_path(bridge_dir)
    page.goto(f"{base_url}/c/{session_id}")
    _ensure_chat_view(page)
    _send(page, "Run two identical commands requiring separate approvals.")
    approve = page.get_by_role("button", name="Approve", exact=True)
    expect(approve).to_be_visible(timeout=120_000)
    approve.click()
    retry = page.get_by_role("button", name="Retry sending approval")
    expect(retry).to_be_visible(timeout=12_000)
    expect(retry).not_to_be_visible(timeout=12_000)
    expect(page.get_by_text("Resolved in Kiro")).to_be_visible()
    expect(approve).to_be_visible(timeout=15_000)
    page.wait_for_timeout(1_000)
    assert _recorder_response_ids(record_file) == {"perm-req-1-1"}
    assert record_file.with_suffix(record_file.suffix + ".enters").read_text().splitlines() == [
        "perm-req-1-1"
    ]
