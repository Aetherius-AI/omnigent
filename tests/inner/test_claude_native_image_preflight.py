"""Fresh-image refusals preserve input before Claude's terminal receives it."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from omnigent.harnesses.claude_native import bridge
from omnigent.inner import claude_native_executor as native
from omnigent.inner.executor import ExecutorConfig, ExecutorError, TurnComplete

_TEXT_MODEL = "managed.text-model"
_IMAGE_MODEL = "managed.image-model"
_IMAGE = "data:image/png;base64,aW1hZ2U="


def _prepared_bridge(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    live_model: str,
    metadata: bool = True,
) -> Path:
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "bridges")
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    options = [
        {"id": _TEXT_MODEL, "input_modalities": ["text"]},
        {"id": _IMAGE_MODEL, "input_modalities": ["text", "image"]},
    ]
    directory = bridge.prepare_bridge_dir(
        "image-test",
        workspace=tmp_path,
        launch_model=_TEXT_MODEL,
        picker_values=[_TEXT_MODEL, _IMAGE_MODEL],
        model_options=options if metadata else [],
    )
    (directory / bridge._CONTEXT_FILE).write_text(json.dumps({"model": live_model}))
    return directory


@pytest.mark.asyncio
@pytest.mark.parametrize("block_type", ["input_image", "input_file"])
async def test_text_only_model_refuses_image_before_materialization_or_injection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, block_type: str
) -> None:
    directory = _prepared_bridge(monkeypatch, tmp_path, live_model=_TEXT_MODEL)

    def unexpected(*_args: object, **_kwargs: object) -> None:
        pytest.fail("refused image reached attachment materialization or terminal control")

    monkeypatch.setattr(native, "attachment_reference_line", unexpected)
    monkeypatch.setattr(native, "inject_user_message", unexpected)
    monkeypatch.setattr(native, "inject_slash_command", unexpected)
    monkeypatch.setattr(native, "kill_session", unexpected)
    image = {"type": block_type, "image_url": _IMAGE}
    if block_type == "input_file":
        image = {"type": block_type, "file_data": _IMAGE}
    content = [image, {"type": "input_text", "text": "Keep this complete message."}]
    original = copy.deepcopy(content)
    executor = native.ClaudeNativeExecutor(directory)
    events = [
        event
        async for event in executor.run_turn(
            [{"role": "user", "content": content}], [], "", ExecutorConfig()
        )
    ]
    assert len(events) == 1 and isinstance(events[0], ExecutorError)
    assert events[0].undelivered and events[0].preserve_session
    assert events[0].code == "unsupported_image_input"
    assert "Choose a model that supports images" in events[0].message
    assert content == original
    assert not await executor.enqueue_session_message("session", content)
    assert not (tmp_path / "data").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("live_model", "override", "metadata", "allowed"),
    [
        (_TEXT_MODEL, _IMAGE_MODEL, True, True),
        (_IMAGE_MODEL, _TEXT_MODEL, True, False),
        (_IMAGE_MODEL, None, True, True),
        ("unlisted-model", None, True, True),
        (_TEXT_MODEL, None, False, True),
    ],
)
async def test_image_preflight_uses_effective_override_live_model_and_unknown_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    live_model: str,
    override: str | None,
    metadata: bool,
    allowed: bool,
) -> None:
    directory = _prepared_bridge(monkeypatch, tmp_path, live_model=live_model, metadata=metadata)
    operations: list[str] = []
    monkeypatch.setattr(native, "attachment_reference_line", lambda *_args: "[Attached image]")
    monkeypatch.setattr(
        native, "inject_user_message", lambda *_args, **_kwargs: operations.append("prompt")
    )

    def switch_model(*_args: object, **_kwargs: object) -> None:
        operations.append("model")
        (directory / bridge._CONTEXT_FILE).write_text(json.dumps({"model": override}))

    monkeypatch.setattr(native, "inject_slash_command", switch_model)
    executor = native.ClaudeNativeExecutor(directory)
    events = [
        event
        async for event in executor.run_turn(
            [{"role": "user", "content": [{"type": "input_image", "image_url": _IMAGE}]}],
            [],
            "",
            ExecutorConfig(model=override),
        )
    ]
    if allowed:
        assert events == [TurnComplete(response=None)]
        assert operations[-1] == "prompt"
    else:
        assert len(events) == 1 and isinstance(events[0], ExecutorError)
        assert events[0].undelivered
        assert operations == []


@pytest.mark.asyncio
async def test_text_input_still_reaches_text_only_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    directory = _prepared_bridge(monkeypatch, tmp_path, live_model=_TEXT_MODEL)
    sent: list[str] = []
    monkeypatch.setattr(
        native, "inject_user_message", lambda *_args, content, **_kwargs: sent.append(content)
    )
    events = [
        event
        async for event in native.ClaudeNativeExecutor(directory).run_turn(
            [{"role": "user", "content": "ordinary text"}], [], ""
        )
    ]
    assert events == [TurnComplete(response=None)]
    assert sent == ["ordinary text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("reported_model", [None, _TEXT_MODEL, "unlisted-model"])
async def test_image_is_not_injected_when_compatible_model_adoption_is_unconfirmed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, reported_model: str | None
) -> None:
    directory = _prepared_bridge(monkeypatch, tmp_path, live_model=_TEXT_MODEL)
    monkeypatch.setattr(native, "_IMAGE_MODEL_CONFIRM_TIMEOUT_S", 0.0)
    monkeypatch.setattr(native, "attachment_reference_line", lambda *_args: "[Attached image]")
    operations: list[str] = []

    def switch_model(*_args: object, **_kwargs: object) -> None:
        operations.append("model-request")
        (directory / bridge._CONTEXT_FILE).write_text(json.dumps({"model": reported_model}))

    monkeypatch.setattr(native, "inject_slash_command", switch_model)
    monkeypatch.setattr(
        native, "inject_user_message", lambda *_args, **_kwargs: operations.append("prompt")
    )
    content = [
        {"type": "input_image", "image_url": _IMAGE},
        {"type": "input_text", "text": "Keep"},
    ]
    original = copy.deepcopy(content)
    events = [
        event
        async for event in native.ClaudeNativeExecutor(directory).run_turn(
            [{"role": "user", "content": content}], [], "", ExecutorConfig(model=_IMAGE_MODEL)
        )
    ]
    assert operations == ["model-request"]
    assert len(events) == 1 and isinstance(events[0], ExecutorError)
    assert events[0].undelivered and events[0].preserve_session
    assert "has not confirmed" in events[0].message
    assert "image was not sent" in events[0].message
    assert content == original


def test_native_resume_preserves_genuine_accepted_turn_failure_history(tmp_path: Path) -> None:
    from omnigent.harnesses.claude_native import main as claude_native

    items = [
        {
            "id": "refused",
            "type": "message",
            "role": "user",
            "response_id": "shared-response",
            "delivery_status": "undelivered",
            "content": [{"type": "input_text", "text": "Unsent image message"}],
        },
        {
            "id": "validation",
            "type": "error",
            "source": "harness",
            "response_id": "shared-response",
            "code": "unsupported_image_input",
            "message": "Image was not sent",
        },
        {
            "id": "accepted",
            "type": "message",
            "role": "user",
            "response_id": "shared-response",
            "content": [{"type": "input_text", "text": "Accepted work that later failed"}],
        },
        {
            "id": "steered",
            "type": "message",
            "role": "user",
            "response_id": "shared-response",
            "content": [{"type": "input_text", "text": "Accepted steering input"}],
        },
        {
            "id": "provider-error",
            "type": "error",
            "source": "harness",
            "response_id": "shared-response",
            "code": "native_turn_error",
            "message": "Provider failed after accepting input",
        },
    ]
    original = copy.deepcopy(items)
    records = claude_native._claude_transcript_records_from_session_items(
        items,
        session_id="session",
        external_session_id="native-session",
        cwd=tmp_path,
        bridge_dir=tmp_path / "bridge",
    )
    assert len(records) == 2
    assert "Accepted work that later failed" in str(records[0]["message"])
    assert "Accepted steering input" in str(records[1]["message"])
    assert "Unsent image message" not in str(records)
    assert items == original
