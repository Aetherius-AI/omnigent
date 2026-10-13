"""Real Claude CLI: a ``skills: none`` bundle can still run its own skill.

Drives ``ClaudeSDKExecutor.run_turn`` in-process against the real ``claude``
binary, with the mock Anthropic endpoint scripting the model's ``Skill`` call,
so the CLI's own skills-allowlist gate produces the verdict.
"""

from __future__ import annotations

import asyncio
import shutil
import uuid
from pathlib import Path

import pytest

from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor
from omnigent.inner.executor import ToolCallComplete, ToolCallStatus
from tests.e2e.conftest import configure_mock_llm, reset_mock_llm

_SKILL_DIR = "probe-skill"
_AGENT_NAME = "probe-agent"
_TURN_TIMEOUT_SEC = 120

# Claude keys a plugin skill by its directory and lists it to the model as
# ``<plugin>:<dir>``; a differing frontmatter label must not change the
# allowlist entry the executor seeds.
_CASES = {
    "label-matches-dir-bare-call": (_SKILL_DIR, _SKILL_DIR),
    "label-differs-qualified-call": ("Probe Skill", f"{_AGENT_NAME}:{_SKILL_DIR}"),
}


def _write_bundle(root: Path, label: str) -> Path:
    skill_dir = root / "bundle" / "skills" / _SKILL_DIR
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {label}\ndescription: Prints a marker line.\n---\n\n"
        "When invoked, output the literal line PROBE-SKILL-BODY.\n"
    )
    return root / "bundle"


@pytest.mark.parametrize(("label", "invoked"), list(_CASES.values()), ids=list(_CASES))
def test_native_skill_tool_runs_bundled_skill_under_skills_none(
    mock_llm_server_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    invoked: str,
) -> None:
    """The CLI runs the bundled skill instead of rejecting it as not allowlisted."""
    if shutil.which("claude") is None:
        pytest.skip("'claude' CLI is not on PATH. Install Claude Code to run this test.")

    bundle = _write_bundle(tmp_path, label)
    token = f"skills-none-{uuid.uuid4().hex[:8]}"
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [{"tool_calls": [{"name": "Skill", "arguments": f'{{"skill": "{invoked}"}}'}]}]
        + [{"text": f"done {token}"}] * 3,
        match=token,
    )
    monkeypatch.setenv("ANTHROPIC_BASE_URL", mock_llm_server_url)

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
    assert "skills allowlist" not in str(skill_results[0].result)
