"""UI journey: a claude-native session on a gateway Opus 5.5 shows its context window.

Reported on a Databricks Sandbox: selecting Opus 5.5 showed a 200K context window
although the model serves 1M. Databricks discovery pins Claude Code's ``opus``
alias to the plain gateway id (``databricks-claude-opus-5-5``), which Claude Code
does not recognize and sizes at its 200K default; the launch config now spells
that pin with Claude Code's ``[1m]`` window marker. This journey pins the same
plain id through the mock anthropic provider config, so the real ``claude`` CLI
launches on it against the mock Anthropic endpoint.

Journey, on the real web SPA against a live server + runner:

1. open a new claude-native session; the Terminal view reaches "connected"
2. open Configure session → model picker and choose the Opus row
3. send a message and wait for the assistant reply
4. hover the composer context ring and read "<used> / <window> tokens"

Regression guard: the window reads 1M for Opus 5.5.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from omnigent.models.claude_model_vocabulary import normalized_model_id
from tests.e2e_ui.conftest import (
    _create_native_claude_session,
    _ensure_runner_online,
    _server_state,
    _temp_omnigent_mock_config,
    set_fallback_mock_llm,
)
from tests.e2e_ui.messages.test_message_render_parity import (
    _ASSISTANT,
    _WORKING,
    _select_view_mode,
)
from tests.e2e_ui.messages.test_native_claude_render_parity import (
    _open_terminal_view,
    _wait_terminal_connected,
)

OPUS_GATEWAY_ID = "databricks-claude-opus-5-5"
EXPECTED_CONTEXT_WINDOW = 1_000_000

_RING = '[data-testid="composer-context-ring"][aria-label$="of context used"]'
_OPUS_ROW = '[role="menuitemcheckbox"][data-model-id="opus"]'
_TURN_TIMEOUT_MS = 120_000
_RING_TIMEOUT_MS = 60_000
_PICKER_ROW_TIMEOUT_S = 90.0


@pytest.fixture
def opus_gateway_claude_session(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str]]:
    """A real Claude CLI whose ``opus`` alias is pinned to the gateway's Opus 5.5 id."""
    httpx.post(
        f"{mock_llm_server_url}/mock/served_models",
        json={"models": [OPUS_GATEWAY_ID]},
        timeout=10.0,
    ).raise_for_status()
    set_fallback_mock_llm(mock_llm_server_url, OPUS_GATEWAY_ID, "OK")
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    with _temp_omnigent_mock_config(
        mock_llm_server_url,
        "claude",
        workflow_owned=bool(_server_state.get("workflow_owned")),
        claude_models={"opus": OPUS_GATEWAY_ID, "default": OPUS_GATEWAY_ID},
    ):
        session_id = _create_native_claude_session(live_server, runner_id)
        try:
            yield (live_server, session_id)
        finally:
            httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
            if respawned is not None:
                respawned.terminate()
                try:
                    respawned.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    respawned.kill()
                    respawned.wait(timeout=5)


def _wait_for_opus_row(base_url: str, session_id: str) -> None:
    """Fail early when the launch config did not pin Opus 5.5 into the picker.

    The row spells the model as Claude Code resolves the alias, window marker
    included, so the comparison folds the marker away.
    """
    deadline = time.monotonic() + _PICKER_ROW_TIMEOUT_S
    rows: list[dict[str, object]] = []
    wanted = normalized_model_id(OPUS_GATEWAY_ID)
    while time.monotonic() < deadline:
        snapshot = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=20.0).json()
        rows = snapshot.get("model_options") or []
        if any(normalized_model_id(str(row.get("model") or "")) == wanted for row in rows):
            return
        time.sleep(2.0)
    pytest.fail(
        f"the picker never offered {OPUS_GATEWAY_ID}; the anthropic provider used by the "
        f"runner must pin opus to it (rows: {rows})"
    )


def _is_recording(request: pytest.FixtureRequest) -> bool:
    return request.config.getoption("--video", default="off") not in (None, "off")


@pytest.mark.timeout(420)
def test_gateway_opus_session_reports_its_1m_context_window(
    request: pytest.FixtureRequest,
    opus_gateway_claude_session: tuple[str, str],
) -> None:
    base_url, session_id = opus_gateway_claude_session
    _wait_for_opus_row(base_url, session_id)

    page: Page = request.getfixturevalue("page")
    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _select_view_mode(page, "Chat")

    page.get_by_test_id("composer-config-gear").click()
    page.get_by_test_id("composer-agent-edit").click()
    opus_row = page.locator(_OPUS_ROW)
    expect(opus_row).to_be_visible(timeout=30_000)
    opus_row.click()
    page.keyboard.press("Escape")

    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill("Reply with exactly: OK")
    page.get_by_role("button", name="Send", exact=True).click()
    expect(page.locator(_ASSISTANT).last).to_contain_text("OK", timeout=_TURN_TIMEOUT_MS)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=_TURN_TIMEOUT_MS)

    ring = page.locator(_RING)
    expect(ring).to_be_visible(timeout=_RING_TIMEOUT_MS)
    ring.hover()
    tooltip = page.get_by_role("tooltip").filter(has_text="tokens")
    expect(tooltip).to_be_visible(timeout=10_000)
    tooltip_text = tooltip.inner_text()
    output_dir = Path(str(request.config.getoption("--output")))
    output_dir.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(output_dir / "opus-context-ring-tooltip.png"))
    if _is_recording(request):
        page.wait_for_timeout(4_000)

    snapshot = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=20.0).json()
    labels = snapshot.get("labels") or {}
    assert snapshot.get("context_window") == EXPECTED_CONTEXT_WINDOW and (
        "/ 1M tokens" in tooltip_text
    ), (
        f"Opus 5.5 session shows context_window={snapshot.get('context_window')!r}, "
        f"tooltip {tooltip_text!r}, llm_model={snapshot.get('llm_model')!r}, "
        f"reported window label={labels.get('omnigent.last_context_window')!r}, "
        f"last_total_tokens={snapshot.get('last_total_tokens')!r}; "
        f"expected {EXPECTED_CONTEXT_WINDOW} ('… / 1M tokens')"
    )
