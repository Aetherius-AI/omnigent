"""E2E: Edit on a harness in the landing picker keeps the open menu while its models load.

The landing picker fetches a host model catalog only for the selected harness.
Clicking Edit on a harness selects it and starts that fetch; the open menu must
stay put, show "Loading models…", and list the models once they arrive, without
a second click. Pi sits under the harness "Other..." flyout, whose open state
lives in the submenu itself, so it is the most exposed case.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Literal

import pytest
from playwright.async_api import Page, Route, async_playwright, expect

from tests.e2e_ui.start_session.test_start_session import (
    _HOST_ID,
    _agents_body,
    _pi_native_agents_body,
    _register_common_routes,
    _run_in_fresh_loop,
)

_CLAUDE_AGENT_ID = "ag_claude_e2e"
_PI_AGENT_ID = "ag_pi_e2e"
_CLAUDE_MODEL_ID = "sonnet"
_PI_MODEL_ID = "example/model-a"

# Counts commits that showed the picker's loading placeholder; reset once the
# picker is ready so only the Edit interaction is measured.
_PLACEHOLDER_SAMPLER_JS = """window.placeholderSightings = 0;
new MutationObserver(() => {
  if (document.querySelector('[data-testid="new-chat-landing-picker-loading"]')) {
    window.placeholderSightings += 1;
  }
}).observe(document, {childList: true, subtree: true});
"""

Activation = Literal["pointer", "keyboard"]


@pytest.mark.parametrize("activation", ["pointer", "keyboard"])
def test_other_harness_flyout_stays_open_while_models_load(
    seeded_session: tuple[str, str], activation: Activation
) -> None:
    """Edit on an "Other..." harness lands on its model list after the catalog loads.

    ``pointer`` clicks Edit on the Pi row. ``keyboard`` opens the flyout by hovering
    "Other..." and reaches Edit with the arrow keys, so the cursor still rests on the
    "Other..." row when Pi's model submenu opens over it.

    :param seeded_session: ``(base_url, session_id)`` from the spawned server.
    :param activation: How Edit on Pi is reached.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_other_flyout_edit(base_url, session_id, activation))


def test_inline_harness_edit_keeps_menu_while_models_load(
    seeded_session: tuple[str, str],
) -> None:
    """Edit on an inline harness keeps the open menu mounted while its catalog loads.

    :param seeded_session: ``(base_url, session_id)`` from the spawned server.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_inline_edit(base_url, session_id))


class _HeldCatalog:
    """Hold one harness's host model catalog until the test releases it."""

    def __init__(self, models: list[dict[str, object]]) -> None:
        self.models = models
        self.requested = asyncio.Event()
        self.released = asyncio.Event()

    async def handle(self, route: Route) -> None:
        self.requested.set()
        await self.released.wait()
        await route.fulfill(json={"models": self.models})


def _instant_catalog(models: list[dict[str, object]]):
    return lambda route: route.fulfill(json={"models": models})


async def _prepare_page(page: Page, session_id: str, *, selected_agent: str) -> None:
    agents = json.loads(_agents_body())["data"] + json.loads(_pi_native_agents_body())["data"]
    await _register_common_routes(
        page,
        created_session_id=session_id,
        create_bodies=[],
        agents_body=json.dumps({"data": agents}),
    )
    # Only the stubbed agents: leftover sessions on the shared server would
    # otherwise be scanned in as extra picker entries.
    await page.route(
        "**/v1/sessions?*visibility=mine*",
        lambda route: route.fulfill(json={"data": [], "has_more": False}),
    )
    # A remembered host workspace, so host model catalogs are fetched.
    await page.add_init_script(
        f"""window.localStorage.setItem("omnigent:last-agent-id", {json.dumps(selected_agent)});
        window.localStorage.setItem(
            "omnigent:recent-workspaces",
            JSON.stringify({{ {json.dumps(_HOST_ID)}: ["/work/repo"] }})
        );"""
    )
    await page.add_init_script(_PLACEHOLDER_SAMPLER_JS)


async def _open_ready_picker(page: Page, selected_label: str) -> None:
    picker = page.get_by_test_id("new-chat-landing-agent-select")
    await expect(picker).to_be_enabled(timeout=30_000)
    await expect(picker).to_have_attribute("aria-label", re.compile(rf"^{selected_label}"))
    await expect(picker).not_to_have_attribute("aria-busy", "true")
    await page.evaluate("window.placeholderSightings = 0")
    await picker.click()
    await expect(picker).to_have_attribute("aria-expanded", "true")


async def _click_edit(page: Page, agent_id: str) -> None:
    await (
        page.get_by_test_id(f"new-chat-landing-agent-config-{agent_id}")
        .get_by_text("Edit", exact=True)
        .click()
    )


async def _expect_menu_kept_until_models_arrive(
    page: Page, catalog: _HeldCatalog, model_id: str
) -> None:
    """The open menu shows the loading row while held, then the released models."""
    await asyncio.wait_for(catalog.requested.wait(), timeout=15)
    await expect(page.get_by_test_id("new-chat-landing-picker-loading")).to_have_count(0)
    models = page.get_by_test_id("new-chat-landing-agent-models")
    await expect(models).to_contain_text("Loading models…")
    catalog.released.set()
    await expect(page.get_by_test_id(f"new-chat-landing-agent-model-{model_id}")).to_be_visible()
    await expect(models).not_to_contain_text("Loading models…")
    assert await page.evaluate("window.placeholderSightings") == 0, (
        "the open menu was replaced by the loading placeholder"
    )


async def _drive_other_flyout_edit(base_url: str, session_id: str, activation: Activation) -> None:
    catalog = _HeldCatalog([{"id": _PI_MODEL_ID, "displayName": "Model A", "isDefault": True}])
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await context.new_page()
        try:
            await _prepare_page(page, session_id, selected_agent=_CLAUDE_AGENT_ID)
            await page.route(
                f"**/v1/hosts/{_HOST_ID}/harnesses/claude-native/model-options",
                _instant_catalog(
                    [{"id": _CLAUDE_MODEL_ID, "displayName": "Sonnet", "isDefault": True}]
                ),
            )
            await page.route(
                f"**/v1/hosts/{_HOST_ID}/harnesses/pi-native/model-options", catalog.handle
            )
            await page.goto(f"{base_url}/")
            await _open_ready_picker(page, "Claude Code")

            other = page.get_by_test_id("new-chat-landing-harness-more")
            if activation == "pointer":
                await other.click()
                await expect(other).to_have_attribute("data-state", "open")
                await _click_edit(page, _PI_AGENT_ID)
            else:
                await other.hover()
                await expect(other).to_have_attribute("data-state", "open")
                # Right Arrow enters the flyout on its first row (Pi); Right Arrow
                # on that row opens Edit.
                await page.keyboard.press("ArrowRight")
                await expect(
                    page.get_by_test_id(f"new-chat-landing-agent-{_PI_AGENT_ID}")
                ).to_be_focused()
                await page.keyboard.press("ArrowRight")

            # While Pi's catalog loads, the flyout and its model submenu stay open.
            await _expect_menu_kept_until_models_arrive(page, catalog, _PI_MODEL_ID)
            await expect(other).to_have_attribute("data-state", "open")
        finally:
            catalog.released.set()
            await context.close()
            await browser.close()


async def _drive_inline_edit(base_url: str, session_id: str) -> None:
    catalog = _HeldCatalog([{"id": _CLAUDE_MODEL_ID, "displayName": "Sonnet", "isDefault": True}])
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await context.new_page()
        try:
            await _prepare_page(page, session_id, selected_agent=_PI_AGENT_ID)
            await page.route(
                f"**/v1/hosts/{_HOST_ID}/harnesses/pi-native/model-options",
                _instant_catalog(
                    [{"id": _PI_MODEL_ID, "displayName": "Model A", "isDefault": True}]
                ),
            )
            await page.route(
                f"**/v1/hosts/{_HOST_ID}/harnesses/claude-native/model-options", catalog.handle
            )
            await page.goto(f"{base_url}/")
            await _open_ready_picker(page, "Pi")
            await _click_edit(page, _CLAUDE_AGENT_ID)
            await _expect_menu_kept_until_models_arrive(page, catalog, _CLAUDE_MODEL_ID)
        finally:
            catalog.released.set()
            await context.close()
            await browser.close()
