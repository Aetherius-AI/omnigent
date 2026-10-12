"""E2E (hermetic): the new-session composer must let Codex Plan mode be armed.

With a Codex (codex-native) agent selected on the New Session landing screen,
the composer's add (+) menu shows a Plan item, but for codex-native it is
disabled and tooltipped "This harness does not support plan mode", so a
plan-first user cannot arm Plan mode before typing the first prompt. The same
Plan toggle works in-session, after the first submit; see
``tests/e2e_ui/chat/test_codex_model_metadata.py``.

Only the server edges the landing screen consults (hosts, agents, model
options) are faked, like the sibling ``test_start_session.py`` and
``test_codex_effort_prelaunch.py``.
"""

from __future__ import annotations

import json
import re
from typing import Any

from playwright.async_api import Route, async_playwright, expect

from tests.e2e_ui.start_session.test_start_session import (
    _HOST_ID,
    _codex_native_agents_body,
    _register_common_routes,
    _run_in_fresh_loop,
)


def test_new_codex_session_can_arm_plan_mode_before_submit(
    seeded_session: tuple[str, str],
) -> None:
    """The landing add-menu Plan item is selectable for a Codex agent."""
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_codex_plan_prelaunch(base_url, session_id))


async def _drive_codex_plan_prelaunch(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context()
        page = await context.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []
            await _register_common_routes(
                page,
                created_session_id=session_id,
                create_bodies=create_bodies,
                agents_body=_codex_native_agents_body(),
            )

            # Neutralize agent discovery so ONLY the stubbed Codex agent feeds
            # the picker — a native agent another test left behind on the
            # shared server would rank ahead and auto-select instead.
            async def handle_agent_scan(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"data": []}),
                )

            await page.route(
                re.compile(r"/v1/sessions\?(?!.*pinned=).*visibility=mine"), handle_agent_scan
            )
            await page.add_init_script(
                f"""window.localStorage.setItem(
                    "omnigent:recent-workspaces",
                    JSON.stringify({{ {_HOST_ID}: ["/work/repo"] }})
                );"""
            )

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            await page.get_by_test_id("new-chat-landing-attach").click()
            await expect(page.get_by_test_id("new-chat-landing-add-menu")).to_be_visible()
            plan_item = page.get_by_test_id("composer-plan-action")
            await expect(plan_item).to_be_visible()

            await expect(plan_item).not_to_have_attribute(
                "title", "This harness does not support plan mode"
            )
            # Radix marks a disabled menu item with an empty-string data-disabled.
            await expect(plan_item).not_to_have_attribute("data-disabled", "")
        finally:
            await context.close()
            await browser.close()
