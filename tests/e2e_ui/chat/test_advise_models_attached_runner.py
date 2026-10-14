"""``sys_advise_models`` on runners that run outside the routing-enabled server process.

The server builds the routing backends. A runner attached from another process —
an ``omnigent host --server`` daemon, or the sibling runner a local deployment
spawns — must still offer the advisor whenever the server it is attached to
reports ``smart_routing_enabled: true``.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
import yaml
from playwright.sync_api import Browser, Page, expect

from tests._helpers.host import isolated_host
from tests._helpers.server_runner import server_runner
from tests._helpers.session import bind_session_runner, bundle_files, post_session_bundle
from tests.e2e_ui.conftest import configure_mock_llm, set_fallback_mock_llm

_JUDGE_MODEL = "routing-judge"
_PROMPT = "Which model should I use for this task: refactor the auth module?"
_RECOMMENDATION = "The advisor recommends gpt-4o-mini for the auth refactor."
_TURN_TIMEOUT_MS = 90_000
_AGENT_YAML = """\
spec_version: 1
name: advisor_probe
prompt: |
  You are a terse assistant. When asked which model to use, consult your
  model advisor tool and report its recommendation verbatim.
executor:
  model: gpt-4o-mini
  config:
    harness: openai-agents
spawn: true
"""


@dataclass(frozen=True)
class RoutingStack:
    base_url: str
    mock_url: str
    sibling_runner_id: str
    host_id: str
    workspace: Path


@pytest.fixture(scope="module")
def routing_stack(
    tmp_path_factory: pytest.TempPathFactory, mock_llm_server_url: str
) -> Iterator[RoutingStack]:
    """A server whose ``llm:`` block builds the OSS judge, plus a sibling runner and a host."""
    root = tmp_path_factory.mktemp("advise-models-stack")
    config = root / "server.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "llm": {
                    "model": _JUDGE_MODEL,
                    "connection": {"base_url": f"{mock_llm_server_url}/v1", "api_key": "mock-key"},
                }
            }
        )
    )
    set_fallback_mock_llm(mock_llm_server_url, _JUDGE_MODEL, '{"model": "gpt-4o-mini"}')
    provider_env = {"OPENAI_BASE_URL": f"{mock_llm_server_url}/v1", "OPENAI_API_KEY": "mock-key"}
    workspace = root / "host-workspace"
    workspace.mkdir()
    with server_runner(
        root / "stack", server_env=provider_env, server_args=["--config", str(config)]
    ) as stack:
        info = httpx.get(f"{stack.base_url}/v1/info", timeout=10)
        info.raise_for_status()
        assert info.json()["smart_routing_enabled"] is True, info.text
        stack.start_runner(env=provider_env)
        with (
            isolated_host(root / "host", stack.base_url, env=provider_env) as host,
            httpx.Client(base_url=stack.base_url, trust_env=False) as client,
        ):
            host.wait_online(client, timeout=90)
            yield RoutingStack(
                base_url=stack.base_url,
                mock_url=mock_llm_server_url,
                sibling_runner_id=stack.runner_id,
                host_id=host.host_id,
                workspace=workspace,
            )


def _create_session(stack: RoutingStack, *, on_host: bool) -> str:
    bundle = bundle_files({"config.yaml": _AGENT_YAML.encode()})
    metadata = {"host_id": stack.host_id, "workspace": str(stack.workspace)} if on_host else None
    with httpx.Client(base_url=stack.base_url, trust_env=False, timeout=120.0) as client:
        created = post_session_bundle(client.post, "/v1/sessions", bundle, metadata=metadata)
        created.raise_for_status()
        session_id = str(created.json()["session_id"])
        if on_host:
            session = client.get(f"/v1/sessions/{session_id}")
            session.raise_for_status()
            runner_id = session.json()["runner_id"]
            assert runner_id, f"host launch did not bind a runner for session {session_id}"
        else:
            runner_id = stack.sibling_runner_id
            bind_session_runner(client.patch, stack.base_url, session_id, runner_id)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            status = client.get(f"/v1/runners/{runner_id}/status")
            if status.status_code == 200 and status.json().get("online") is True:
                return session_id
            time.sleep(0.5)
    raise AssertionError(f"runner {runner_id} for session {session_id} never came online")


def _script_advisor_call(mock_url: str, nonce: str) -> None:
    arguments = {
        "tasks": [{"title": "auth-refactor", "agents": [{"agent": "worker", "models": None}]}]
    }
    configure_mock_llm(
        mock_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": f"call-{nonce}",
                        "name": "sys_advise_models",
                        "arguments": json.dumps(arguments),
                    }
                ]
            },
            {"text": _RECOMMENDATION},
        ],
        match=nonce,
        required_tools=["sys_list_models"],
    )


def _offered_tools(mock_url: str, nonce: str) -> set[str]:
    """Tool names the runner advertised on the model request carrying ``nonce``."""
    captured = httpx.get(f"{mock_url}/mock/requests", timeout=10)
    captured.raise_for_status()
    requests = captured.json()["requests"]
    for request in requests:
        if nonce in json.dumps(request) and request.get("tools"):
            return {
                str(name).split("__")[-1]
                for tool in request["tools"]
                if (name := tool.get("name") or tool.get("function", {}).get("name"))
            }
    raise AssertionError(
        f"no model request with tools carried {nonce!r}: {len(requests)} captured"
    )


def _send_prompt(page: Page, base_url: str, session_id: str, prompt: str) -> None:
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(prompt)
    page.get_by_role("button", name="Send", exact=True).click()


def _settle_turn(page: Page) -> str | None:
    """Wait for the reply or the error pill; return the expanded error text, if any."""
    pill = page.get_by_test_id("error-pill").first
    reply = page.locator('[data-testid="message-bubble"][data-role="assistant"]').filter(
        has_text=_RECOMMENDATION
    )
    expect(pill.or_(reply)).to_be_visible(timeout=_TURN_TIMEOUT_MS)
    if not pill.is_visible():
        return None
    headline = pill.get_by_test_id("error-headline").inner_text()
    pill.get_by_role("button").first.click()
    content = pill.get_by_test_id("error-message-content")
    expect(content).to_be_visible()
    return f"{headline} | {content.inner_text()}"


@pytest.mark.timeout(240)
@pytest.mark.parametrize("on_host", [True, False], ids=["host-daemon", "sibling-runner"])
def test_attached_runner_offers_advise_models_when_server_routes(
    browser: Browser, routing_stack: RoutingStack, on_host: bool
) -> None:
    """A runner outside the server process still advertises the advisor the server can answer."""
    nonce = f"ref-{uuid.uuid4().hex[:8]}"
    session_id = _create_session(routing_stack, on_host=on_host)
    _script_advisor_call(routing_stack.mock_url, nonce)
    context = browser.new_context()
    try:
        page = context.new_page()
        _send_prompt(page, routing_stack.base_url, session_id, f"{_PROMPT} ({nonce})")
        error_text = _settle_turn(page)
        info = httpx.get(f"{routing_stack.base_url}/v1/info", timeout=10)
        info.raise_for_status()
        routing = info.json()
        tools = _offered_tools(routing_stack.mock_url, nonce)
        assert "sys_list_models" in tools, sorted(tools)
        assert "sys_advise_models" in tools, (
            f"server reports smart_routing_enabled={routing['smart_routing_enabled']} "
            f"(sources {routing['smart_routing_sources']}) but the runner offered "
            f"{sorted(tools)}; chat error: {error_text!r}"
        )
        assert error_text is None, error_text
        expect(
            page.locator('[data-testid="message-bubble"][data-role="assistant"]').last
        ).to_contain_text(_RECOMMENDATION)
    finally:
        context.close()
