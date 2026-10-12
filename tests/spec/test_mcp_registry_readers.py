"""Registry declarations remain readable before gateway execution is available."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yaml

from omnigent.errors import OmnigentError
from omnigent.harnesses.opencode_native.provider import build_opencode_mcp_block
from omnigent.runner.mcp_manager import RunnerMcpManager
from omnigent.spec import load, validate
from omnigent.spec.omnigent import agent_def_to_agent_spec, agent_spec_to_agent_def
from omnigent.spec.types import AgentSpec, MCPServerConfig
from omnigent.tools.mcp import McpServerConnection


def _write_agent(root: Path, layout: str, declaration: dict) -> None:
    config = {
        "spec_version": 1,
        "name": "reader-test",
        "executor": {
            "type": "omnigent",
            "model": "test-model",
            "config": {"harness": "claude-sdk"},
        },
    }
    if layout == "files":
        mcp_dir = root / "tools" / "mcp"
        mcp_dir.mkdir(parents=True)
        (mcp_dir / "tracker.yaml").write_text(yaml.safe_dump({"name": "tracker", **declaration}))
    else:
        config["tools"] = {"tracker": {"type": "mcp", **declaration}}
    filename = "config.yaml"
    if layout == "legacy":
        del config["spec_version"]
        config["prompt"] = "Use the selected tools."
        config["executor"] = {"model": "test-model", "harness": "claude-sdk"}
        filename = "agent.yaml"
    (root / filename).write_text(yaml.safe_dump(config))


@pytest.mark.parametrize("layout", ["files", "inline", "legacy"])
@pytest.mark.parametrize("allowed", [None, [], ["whoami"]])
def test_registry_readers_preserve_reference_and_restrictions(tmp_path, layout, allowed):
    _write_agent(
        tmp_path,
        layout,
        {"transport": "registry", "description": "Issue tracker", "tools": allowed},
    )
    spec = load(tmp_path, expand_env=False)
    expected = MCPServerConfig(
        name="tracker", transport="registry", description="Issue tracker", tools=allowed
    )
    assert spec.mcp_servers == [expected]
    restored = agent_def_to_agent_spec(agent_spec_to_agent_def(spec))
    assert restored.mcp_servers == [expected]


def test_nested_registry_reference_is_not_pruned_on_execution_load(tmp_path):
    _write_agent(tmp_path, "inline", {"transport": "registry", "tools": []})
    child = tmp_path / "agents" / "child"
    child.mkdir(parents=True)
    _write_agent(child, "files", {"transport": "registry", "tools": ["whoami"]})
    spec = load(tmp_path, expand_env=False, prune_invalid_sub_agents=True)
    assert len(spec.sub_agents) == 1
    assert spec.sub_agents[0].mcp_servers == [
        MCPServerConfig(name="tracker", transport="registry", tools=["whoami"])
    ]


@pytest.mark.parametrize("layout", ["files", "inline", "legacy"])
@pytest.mark.parametrize(
    "field",
    [
        "url",
        "command",
        "args",
        "env",
        "headers",
        "auth",
        "profile",
        "databricks_server",
        "tool_name",
    ],
)
def test_registry_readers_reject_connection_settings(tmp_path, layout, field):
    _write_agent(tmp_path, layout, {"transport": "registry", field: None})
    with pytest.raises((OmnigentError, ValueError), match="cannot override"):
        load(tmp_path, expand_env=False)


def test_programmatic_registry_declaration_rejects_connection_settings():
    spec = AgentSpec(
        spec_version=1,
        mcp_servers=[MCPServerConfig(name="tracker", transport="registry", command="")],
    )
    assert "cannot override" in str(validate(spec).errors)


async def test_registry_discovery_fails_clearly_without_opening_a_transport(monkeypatch):
    open_http = AsyncMock(side_effect=AssertionError("unexpected HTTP transport"))
    open_stdio = AsyncMock(side_effect=AssertionError("unexpected stdio transport"))
    monkeypatch.setattr(McpServerConnection, "_open_http_transport", open_http)
    monkeypatch.setattr(McpServerConnection, "_open_stdio_transport", open_stdio)
    spec = AgentSpec(
        spec_version=1, mcp_servers=[MCPServerConfig(name="tracker", transport="registry")]
    )
    manager = RunnerMcpManager()
    try:
        result = await manager.schemas_for(spec)
        assert result.schemas == []
        assert result.tool_names == set()
        assert "requires a gateway-capable Omnigent runtime" in result.failures["tracker"]
        with pytest.raises(RuntimeError, match="requires a gateway-capable"):
            await manager.call_tool(spec, "tracker__whoami", {})
    finally:
        await manager.shutdown()
    open_http.assert_not_called()
    open_stdio.assert_not_called()


def test_native_direct_configuration_rejects_registry_execution():
    with pytest.raises(RuntimeError, match="requires a gateway-capable"):
        build_opencode_mcp_block([MCPServerConfig(name="tracker", transport="registry")])
