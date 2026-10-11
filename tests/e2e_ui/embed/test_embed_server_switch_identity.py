"""An embedded host that switches Servers in place must get a fresh identity on the new Server.

Part of the gated e2e suite; see this package's ``conftest`` module docstring.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from urllib.parse import urlencode

import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.embed._embed_host_harness import (
    SERVER_A_STATUS,
    SERVER_A_USER,
    SERVER_B_STATUS,
    SERVER_B_USER,
    EmbedHost,
    embed_host,
)

_TRANSPORT_WAIT_MS = 15_000
_IDENTITY_SETTLE_MS = 10_000


@pytest.fixture(scope="module")
def embed_host_env(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[EmbedHost]:
    with embed_host(
        tmp_path_factory.mktemp("embed_host"),
        rebuild_host_page=not request.config.getoption("--ui-skip-build"),
    ) as host:
        yield host


def open_host_page(page: Page, host: EmbedHost, *, me_delay_ms: int = 0) -> None:
    """Load the host page connected to Server A and wait for the embedded sidebar."""
    query: dict[str, str] = {"server": "a"}
    if me_delay_ms:
        query["meDelayA"] = str(me_delay_ms)
    page.goto(f"{host.url}/?{urlencode(query)}")
    expect(page.get_by_test_id("host-status")).to_have_text(SERVER_A_STATUS)
    expect(page.get_by_test_id("settings-button")).to_be_visible()


def wait_for_answer(page: Page, server: str, path_prefix: str) -> None:
    """Wait until the host transport has handed the embed a response from *server*."""
    page.wait_for_function(
        "([server, prefix]) => window.omnigentE2EHost.answered[server]"
        ".some((r) => r.path.startsWith(prefix))",
        arg=[server, path_prefix],
        timeout=_TRANSPORT_WAIT_MS,
    )


def sent_to(page: Page, server: str) -> list[dict[str, Any]]:
    return page.evaluate("(server) => window.omnigentE2EHost.sent[server]", server)


def answered_by(page: Page, server: str) -> list[dict[str, Any]]:
    return page.evaluate("(server) => window.omnigentE2EHost.answered[server]", server)


def embed_identity(page: Page) -> dict[str, Any]:
    return page.evaluate("() => window.omnigentE2EHost.identity()")


def connect_server_b(page: Page) -> None:
    page.get_by_test_id("host-connect-b").click()
    expect(page.get_by_test_id("host-status")).to_have_text(SERVER_B_STATUS)


def test_admin_chrome_follows_the_new_servers_user(
    request: pytest.FixtureRequest, embed_host_env: EmbedHost
) -> None:
    page: Page = request.getfixturevalue("page")
    open_host_page(page, embed_host_env)
    wait_for_answer(page, "a", "/v1/me")
    page.get_by_test_id("settings-button").click()
    expect(page.get_by_test_id("settings-nav-members")).to_be_visible()
    expect(page.get_by_test_id("settings-nav-policies")).to_be_visible()

    connect_server_b(page)
    wait_for_answer(page, "b", "/v1/sessions")

    expect(page.get_by_test_id("settings-nav-general")).to_be_visible()
    expect(page.get_by_test_id("settings-nav-policies")).to_have_count(
        0, timeout=_IDENTITY_SETTLE_MS
    )
    expect(page.get_by_test_id("settings-nav-members")).to_have_count(0)


def test_identity_is_resolved_on_the_new_server(
    request: pytest.FixtureRequest, embed_host_env: EmbedHost
) -> None:
    page: Page = request.getfixturevalue("page")
    open_host_page(page, embed_host_env)
    wait_for_answer(page, "a", "/v1/me")

    connect_server_b(page)
    wait_for_answer(page, "b", "/v1/sessions")

    assert embed_identity(page) == {
        "user": SERVER_B_USER,
        "isAdmin": False,
        "serverIdentity": "server-b",
    }
    requests_to_b = sent_to(page, "b")
    assert any(r["path"].startswith("/v1/me") for r in requests_to_b), requests_to_b
    stamped_alice = [r["path"] for r in requests_to_b if r["embedUser"] == SERVER_A_USER]
    assert stamped_alice == [], stamped_alice


def test_late_answer_from_the_previous_server_is_ignored(
    request: pytest.FixtureRequest, embed_host_env: EmbedHost
) -> None:
    page: Page = request.getfixturevalue("page")
    open_host_page(page, embed_host_env, me_delay_ms=4000)
    page.wait_for_function(
        "() => window.omnigentE2EHost.sent.a.some((r) => r.path.startsWith('/v1/me'))",
        timeout=_TRANSPORT_WAIT_MS,
    )
    assert not any(r["path"].startswith("/v1/me") for r in answered_by(page, "a"))

    connect_server_b(page)
    wait_for_answer(page, "a", "/v1/me")
    wait_for_answer(page, "b", "/v1/sessions")

    page.get_by_test_id("settings-button").click()
    expect(page.get_by_test_id("settings-nav-general")).to_be_visible()
    expect(page.get_by_test_id("settings-nav-policies")).to_have_count(
        0, timeout=_IDENTITY_SETTLE_MS
    )
    assert embed_identity(page) == {
        "user": SERVER_B_USER,
        "isAdmin": False,
        "serverIdentity": "server-b",
    }
