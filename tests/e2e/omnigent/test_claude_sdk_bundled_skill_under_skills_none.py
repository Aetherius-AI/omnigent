"""Real Claude CLI: a ``skills: none`` bundle can still run its own skill.

Drives ``ClaudeSDKExecutor.run_turn`` in-process against the real ``claude``
binary, with the mock Anthropic endpoint scripting the model's ``Skill`` call,
so the CLI's own skills-allowlist gate produces the verdict.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import claude_agent_sdk
import pytest

from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor, _find_system_claude
from omnigent.inner.executor import ToolCallComplete, ToolCallStatus
from tests.e2e.conftest import configure_mock_llm, reset_mock_llm

_AGENT_NAME = "probe-agent"
_TURN_TIMEOUT_SEC = 120

# Claude registers a plugin skill as ``<plugin>:<dir>`` with the directory
# name normalized to ``[A-Za-z0-9_-]``; the frontmatter label never changes
# the allowlist entry the executor must seed.
_CASES = {
    "label-matches-dir-bare-call": ("probe-skill", "probe-skill", "probe-skill"),
    "label-differs-qualified-call": ("probe-skill", "Probe Skill", f"{_AGENT_NAME}:probe-skill"),
    "dotted-dir-normalized": ("probe.skill", "probe-skill", f"{_AGENT_NAME}:probe-skill"),
}


def _claude_cli_available() -> bool:
    """The executor prefers a system ``claude``; the SDK falls back to its bundled CLI."""
    bundled = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
    return _find_system_claude() is not None or bundled.is_file()


def _write_bundle(root: Path, skill_dir_name: str, label: str) -> Path:
    skill_dir = root / "bundle" / "skills" / skill_dir_name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {label}\ndescription: Prints a marker line.\n---\n\n"
        "When invoked, output the literal line PROBE-SKILL-BODY.\n"
    )
    return root / "bundle"


@pytest.mark.parametrize(
    ("skill_dir_name", "label", "invoked"), list(_CASES.values()), ids=list(_CASES)
)
def test_native_skill_tool_runs_bundled_skill_under_skills_none(
    mock_llm_server_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    skill_dir_name: str,
    label: str,
    invoked: str,
) -> None:
    """The CLI runs the bundled skill instead of rejecting it as not allowlisted."""
    if not _claude_cli_available():
        pytest.skip("No Claude Code CLI (system or SDK-bundled) is available.")

    bundle = _write_bundle(tmp_path, skill_dir_name, label)
    token = f"skills-none-{uuid.uuid4().hex[:8]}"
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [{"tool_calls": [{"name": "Skill", "arguments": f'{{"skill": "{invoked}"}}'}]}]
        + [{"text": f"done {token}"}] * 3,
        match=token,
    )
    monkeypatch.setenv("ANTHROPIC_BASE_URL", mock_llm_server_url)
    # The tight CLI sandbox is rooted at the bundle cwd and would hide this
    # test's venv from its launcher; the allowlist gate does not depend on it.
    monkeypatch.setenv("OMNIGENT_CLAUDE_SDK_NO_SANDBOX", "1")

    executor = ClaudeSDKExecutor(
        cwd=str(bundle),
        bundle_dir=bundle,
        agent_name=_AGENT_NAME,
        skills_filter="none",
        permission_mode="bypassPermissions",
        api_key_helper="printf %s mock-key",
    )

    async def _run() -> list[object]:
        try:
            return [
                event
                async for event in executor.run_turn(
                    [{"role": "user", "content": f"Run your bundled skill {invoked}. {token}"}],
                    [],
                    "You are a test agent.",
                )
            ]
        finally:
            await executor.close()

    events = asyncio.run(asyncio.wait_for(_run(), timeout=_TURN_TIMEOUT_SEC))

    skill_results = [e for e in events if isinstance(e, ToolCallComplete) and e.name == "Skill"]
    assert skill_results, f"the scripted Skill call never completed: {events}"
    assert skill_results[0].status == ToolCallStatus.SUCCESS, skill_results[0]
