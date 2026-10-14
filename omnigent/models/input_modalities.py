"""Optional, exact-model input metadata supplied by a managed catalog."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

CLAUDE_MODEL_INPUT_MODALITIES_ENV = "OMNIGENT_CLAUDE_MODEL_INPUT_MODALITIES"
IMAGE_INPUT_REFUSED_CODE = "unsupported_image_input"
_MAX_METADATA_BYTES = 64 * 1024
_MAX_MODELS = 256
_MAX_MODEL_ID_LENGTH = 256
_MODALITIES = frozenset({"text", "image"})


def validated_input_modalities(value: object) -> tuple[str, ...] | None:
    """Return explicit known modalities; malformed metadata remains unknown."""
    if not isinstance(value, list) or not 1 <= len(value) <= len(_MODALITIES):
        return None
    if any(not isinstance(item, str) or item not in _MODALITIES for item in value):
        return None
    if len(set(value)) != len(value):
        return None
    return tuple(sorted(value))


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate model metadata key")
        result[key] = value
    return result


def parse_model_input_modalities(raw: object) -> dict[str, tuple[str, ...]]:
    """Read a bounded JSON map without guessing capabilities for unknown rows."""
    if not isinstance(raw, str) or len(raw) > _MAX_METADATA_BYTES:
        return {}
    try:
        if len(raw.encode("utf-8")) > _MAX_METADATA_BYTES:
            return {}
        payload = json.loads(raw, object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError, RecursionError):
        return {}
    if not isinstance(payload, dict) or len(payload) > _MAX_MODELS:
        return {}
    result: dict[str, tuple[str, ...]] = {}
    for model, value in payload.items():
        if (
            not isinstance(model, str)
            or not model
            or len(model) > _MAX_MODEL_ID_LENGTH
            or model != model.strip()
            or any(ord(char) < 32 for char in model)
        ):
            continue
        modalities = validated_input_modalities(value)
        if modalities is not None:
            result[model] = modalities
    return result


def model_input_modalities(
    options: Sequence[Mapping[str, object]], model: str | None
) -> tuple[str, ...] | None:
    """Resolve only an exact catalog id/model match, treating conflicts as unknown."""
    if model is None:
        return None
    matches = {
        validated_input_modalities(option.get("input_modalities"))
        for option in options
        if model in (option.get("id"), option.get("model"))
    }
    return matches.pop() if len(matches) == 1 else None


def model_modality_options(raw: object) -> list[dict[str, object]]:
    """Retain bounded, validated capability rows without adding model choices."""
    if not isinstance(raw, (list, tuple)) or len(raw) > _MAX_MODELS:
        return []
    result: list[dict[str, object]] = []
    for option in raw:
        if not isinstance(option, dict):
            continue
        modalities = validated_input_modalities(option.get("input_modalities"))
        row: dict[str, object] = {}
        if modalities is not None:
            row["input_modalities"] = list(modalities)
        for key in ("id", "model"):
            value = option.get(key)
            if (
                isinstance(value, str)
                and value
                and len(value) <= _MAX_MODEL_ID_LENGTH
                and value == value.strip()
                and not any(ord(char) < 32 for char in value)
            ):
                row[key] = value
        if "id" in row or "model" in row:
            result.append(row)
    return result


def content_has_image(content: object) -> bool:
    """Recognize fresh image blocks before any attachment is read or materialized."""
    if not isinstance(content, list):
        return False
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "input_image":
            return True
        if block.get("type") == "input_file":
            data = block.get("file_data")
            if isinstance(data, str) and data.lower().startswith("data:image/"):
                return True
    return False


def image_model_refusal(model: str) -> str:
    """Explain a refused input without changing the selected model or its content."""
    return (
        f"{model} supports text only and cannot accept this image. "
        "Choose a model that supports images, then resend the complete message."
    )
