"""E2E: New Chat offers the selected host's Cursor models before the first message.

Journey: a server plus a separately registered execution host whose
``cursor-agent`` is installed and signed in. In New Chat the user selects that
host, picks **Cursor** in the harness picker and looks for a model control
before sending anything. Like Claude Code, Codex, Pi and Devin, the Cursor row
must offer **Edit → Models** listing what that host's ``cursor-agent models``
prints, and the host must answer
``GET /v1/hosts/{id}/harnesses/cursor-native/model-options`` with that catalog.

The rig runs a real ``omnigent host`` daemon against the suite's server with a
scripted, signed-in ``cursor-agent`` double first on its PATH (the real CLI
needs a Cursor login). Server, SPA, host daemon and the host→server catalog
frames are the real product.

Run (SPA built; ``OMNIGENT_E2E_CURSOR_HOST_HOME`` pins the host identity dir)::

    pytest tests/e2e_ui/start_session/test_cursor_native_prelaunch_picker.py --ui-skip-build
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
import yaml
from playwright.sync_api import Page, expect

from omnigent.harnesses.cursor_native.main import parse_cursor_cli_model_options
from tests.e2e_ui.start_session.test_native_picker_cli_parity import (
    _MODELS_SECTION_TESTID,
    _dismiss_setup_review,
    _open_agent_menu,
    _sanitized_env,
    _wait_for,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_HOST_READY_TIMEOUT_S = 180.0
_CATALOG_TIMEOUT_MS = 60_000

_CURSOR_MODELS_OUTPUT = """Available models

auto - Auto (default)
gpt-5.2 - GPT-5.2
gpt-5.2-high - GPT-5.2 High
claude-4.5-sonnet - Claude 4.5 Sonnet
claude-4.5-sonnet-thinking - Claude 4.5 Sonnet Thinking
gemini-3-pro - Gemini 3 Pro
composer-1 - Composer 1 (current)
"""

# Signed-in Cursor CLI double: version above the harness minimum, authenticated
# status, a fixed `cursor-agent models` listing, and a TUI launch that logs its
# argv and stays attached. The real CLI cannot list models without a login.
_CURSOR_AGENT_DOUBLE = '''#!/usr/bin/env python3
import json
import os
import sys
import time

ARGS = sys.argv[1:]
VERSION = "2026.09.26-dd393fe"
MODELS = """__MODELS__"""

log_path = os.environ.get("CURSOR_AGENT_DOUBLE_LOG")
if log_path:
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"argv": ARGS, "cwd": os.getcwd(), "time": time.time()}) + "\\n")

if "--version" in ARGS or "-v" in ARGS:
    print(VERSION)
    raise SystemExit(0)
if ARGS[:1] == ["status"]:
    print(json.dumps({"status": "authenticated", "isAuthenticated": True,
                      "hasAccessToken": True, "hasRefreshToken": True, "version": VERSION}))
    raise SystemExit(0)
if ARGS[:1] == ["models"]:
    sys.stdout.write(MODELS)
    raise SystemExit(0)
if ARGS[:1] in (["login"], ["logout"]):
    print("stub cursor-agent: " + ARGS[0] + " ok")
    raise SystemExit(0)

print("stub cursor-agent TUI started with: " + " ".join(ARGS))
sys.stdout.flush()
try:
    while sys.stdin.readline():
        pass
except KeyboardInterrupt:
    pass
'''.replace("__MODELS__", _CURSOR_MODELS_OUTPUT)


@dataclass(frozen=True)
class CursorHostRig:
    """A registered, online host whose Cursor CLI is the signed-in double."""

    base_url: str
    host_id: str
    host_log: Path
    launch_log: Path
    expected_models: list[dict[str, object]]

    def log_tail(self) -> str:
        if not self.host_log.exists():
            return ""
        return self.host_log.read_text(errors="replace")[-3000:]


def _hosts(base_url: str) -> list[dict]:
    return httpx.get(f"{base_url}/v1/hosts", timeout=10).json().get("hosts", [])


def _host_identity(config_home: Path) -> str | None:
    config_path = config_home / "config.yaml"
    if not config_path.exists():
        return None
    data = yaml.safe_load(config_path.read_text()) or {}
    host = data.get("host") if isinstance(data, dict) else None
    host_id = host.get("host_id") if isinstance(host, dict) else None
    return str(host_id) if host_id else None


@contextlib.contextmanager
def cursor_host(base_url: str, root: Path) -> Iterator[CursorHostRig]:
    """Register ``root`` as a host of ``base_url`` with the Cursor double on PATH."""
    stub_bin = root / "bin"
    config_home = root / "config"
    data_dir = root / "data"
    for path in (stub_bin, config_home, data_dir):
        path.mkdir(parents=True, exist_ok=True)
    stub = stub_bin / "cursor-agent"
    stub.write_text(_CURSOR_AGENT_DOUBLE)
    stub.chmod(0o755)
    launch_log = root / "cursor-agent-invocations.jsonl"
    host_log = root / "host.log"

    env = _sanitized_env()
    env["PATH"] = f"{stub_bin}{os.pathsep}{os.environ['PATH']}"
    env["OMNIGENT_CONFIG_HOME"] = str(config_home)
    env["OMNIGENT_DATA_DIR"] = str(data_dir)
    env["CURSOR_AGENT_DOUBLE_LOG"] = str(launch_log)
    handle = open(host_log, "w")  # noqa: SIM115 — subprocess lifetime
    host = subprocess.Popen(
        [sys.executable, "-m", "omnigent.cli", "host", "--server", base_url, "--no-open"],
        env=env,
        cwd=str(_REPO_ROOT),
        stdout=handle,
        stderr=subprocess.STDOUT,
    )
    try:

        def _ready() -> str | None:
            if host.poll() is not None:
                raise AssertionError(
                    f"host daemon exited early:\n{host_log.read_text(errors='replace')[-3000:]}"
                )
            host_id = _host_identity(config_home)
            if host_id is None:
                return None
            for row in _hosts(base_url):
                readiness = row.get("configured_harnesses") or {}
                if row.get("host_id") == host_id and row.get("status") == "online":
                    if readiness.get("cursor-native") is True:
                        return host_id
                    raise AssertionError(
                        f"host {host_id} is online but reports cursor-native readiness "
                        f"{readiness.get('cursor-native')!r}; the double on PATH should "
                        "read as installed and signed in"
                    )
            return None

        host_id = _wait_for(_ready, _HOST_READY_TIMEOUT_S, "the Cursor host to come online")
        yield CursorHostRig(
            base_url=base_url,
            host_id=str(host_id),
            host_log=host_log,
            launch_log=launch_log,
            expected_models=list(parse_cursor_cli_model_options(_CURSOR_MODELS_OUTPUT)),
        )
    finally:
        if host.poll() is None:
            host.terminate()
            try:
                host.wait(timeout=15)
            except subprocess.TimeoutExpired:
                host.kill()
                host.wait(timeout=5)
        handle.close()


@pytest.fixture(scope="module")
def cursor_rig(
    live_server: str, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[CursorHostRig]:
    pinned = os.environ.get("OMNIGENT_E2E_CURSOR_HOST_HOME")
    root = Path(pinned) if pinned else tmp_path_factory.mktemp("cursor_host")
    with cursor_host(live_server, root) as rig:
        yield rig


def _agent_id(base_url: str, name: str) -> str:
    rows = httpx.get(f"{base_url}/v1/agents", timeout=10).json().get("data", [])
    for row in rows:
        if row.get("name") == name:
            return str(row["id"])
    raise AssertionError(f"{name!r} is not offered by {base_url}/v1/agents")


def _select_host_workspace(page: Page) -> None:
    """Make the registered host's folder the composer workspace through the chip."""
    page.get_by_test_id("new-chat-landing-workspace-chip").click()
    page.get_by_test_id("new-chat-landing-workspace-open-folder").click()
    expect(page.get_by_test_id("workspace-picker")).to_be_visible(timeout=30_000)
    page.get_by_test_id("workspace-picker-select").click()
    expect(page.get_by_test_id("workspace-picker")).to_be_hidden()


def _select_harness(page: Page, agent_id: str) -> None:
    _open_agent_menu(page)
    row = page.get_by_test_id(f"new-chat-landing-agent-{agent_id}")
    if row.count() == 0:
        page.get_by_test_id("new-chat-landing-harness-more").click()
    expect(row).to_be_visible(timeout=10_000)
    row.click()
    page.wait_for_timeout(800)


def _close_menus(page: Page) -> None:
    for _ in range(2):
        if page.get_by_role("menu").count() == 0:
            break
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)


def test_new_chat_offers_the_hosts_cursor_models_before_launch(
    request: pytest.FixtureRequest, cursor_rig: CursorHostRig
) -> None:
    """Selecting Cursor offers Edit → Models from the host's cursor-agent catalog."""
    cursor_id = _agent_id(cursor_rig.base_url, "cursor-native-ui")
    catalog_requests: list[str] = []

    page: Page = request.getfixturevalue("page")
    page.on(
        "request",
        lambda req: catalog_requests.append(req.url) if "/model-options" in req.url else None,
    )
    page.goto(cursor_rig.base_url)
    expect(page.get_by_test_id("new-chat-landing-input")).to_be_visible(timeout=30_000)
    _dismiss_setup_review(page)
    _select_host_workspace(page)

    _select_harness(page, cursor_id)
    _close_menus(page)
    # The trigger shows the picked/default model once a catalog exists; the
    # harness is named in its accessible label, like the other model pickers.
    expect(page.get_by_test_id("new-chat-landing-agent-select")).to_have_attribute(
        "aria-label", re.compile(r"^Cursor\b")
    )

    # Reopen the picker on the selected Cursor row and look for its model control.
    _open_agent_menu(page)
    row = page.get_by_test_id(f"new-chat-landing-agent-{cursor_id}")
    if row.count() == 0:
        page.get_by_test_id("new-chat-landing-harness-more").click()
    expect(row).to_be_visible(timeout=10_000)
    expect(row).not_to_have_attribute("aria-disabled", "true")
    page.wait_for_timeout(1_500)

    edit = page.get_by_test_id(f"new-chat-landing-agent-config-{cursor_id}")
    cursor_catalog_requests = [
        url
        for url in catalog_requests
        if f"/hosts/{cursor_rig.host_id}/harnesses/cursor-native/" in url
    ]
    assert edit.count() == 1, (
        "the selected Cursor row offers no Edit / model control before the first message "
        f"(config entries: {edit.count()}, models section present: "
        f"{page.get_by_test_id(_MODELS_SECTION_TESTID).count() > 0}); catalog requests the "
        f"page issued: {sorted(set(catalog_requests))}; cursor-native requests: "
        f"{cursor_catalog_requests}\n{cursor_rig.log_tail()}"
    )
    edit.get_by_text("Edit", exact=True).click()
    models = page.get_by_test_id(_MODELS_SECTION_TESTID)
    expect(models).to_be_visible(timeout=_CATALOG_TIMEOUT_MS)
    for model in cursor_rig.expected_models:
        expect(page.get_by_test_id(f"new-chat-landing-agent-model-{model['id']}")).to_be_visible(
            timeout=_CATALOG_TIMEOUT_MS
        )
    assert cursor_catalog_requests, (
        "New Chat never requested the host's cursor-native catalog; requests issued: "
        f"{sorted(set(catalog_requests))}"
    )


def test_host_serves_the_cursor_catalog_before_launch(cursor_rig: CursorHostRig) -> None:
    """The pre-launch host endpoint lists what the host's `cursor-agent models` prints."""
    response = httpx.get(
        f"{cursor_rig.base_url}/v1/hosts/{cursor_rig.host_id}/harnesses/cursor-native/model-options",
        timeout=60,
    )
    assert response.status_code == 200, (
        f"GET …/harnesses/cursor-native/model-options -> HTTP {response.status_code} "
        f"{response.text[:300]}\n{cursor_rig.log_tail()}"
    )
    offered = {row["id"] for row in response.json()["models"]}
    expected = {row["id"] for row in cursor_rig.expected_models}
    assert offered == expected, json.dumps(response.json(), indent=1)[:1500]
