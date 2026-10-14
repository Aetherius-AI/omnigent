"""Managed input metadata stays exact, bounded, and unknown when malformed."""

from __future__ import annotations

import json

import pytest

from omnigent.models.input_modalities import (
    model_input_modalities,
    model_modality_options,
    parse_model_input_modalities,
    validated_input_modalities,
)


@pytest.mark.parametrize(
    "value",
    [None, True, "image", (), [], {}, ["audio"], ["Text"], ["text", True], ["text", "text"]],
)
def test_invalid_input_modalities_remain_unknown(value: object) -> None:
    assert validated_input_modalities(value) is None


def test_metadata_parsing_retains_only_valid_exact_ids() -> None:
    raw = json.dumps(
        {
            "system.ai.glm-5-3": ["text"],
            "system.ai.kimi-k3": ["text", "image"],
            "system.ai.glm-5-3[1m]": ["image"],
            "invalid": ["audio"],
            "": ["image"],
            " padded": ["image"],
            "control\n": ["image"],
            "x" * 257: ["image"],
        }
    )
    assert parse_model_input_modalities(raw) == {
        "system.ai.glm-5-3": ("text",),
        "system.ai.kimi-k3": ("image", "text"),
        "system.ai.glm-5-3[1m]": ("image",),
    }


@pytest.mark.parametrize(
    "raw",
    [
        None,
        {"model": ["text"]},
        "",
        "{",
        "[]",
        "null",
        '{"model":["text"],"model":["text","image"]}',
        "\ud800",
        "[" * 1100 + "0" + "]" * 1100,
    ],
)
def test_malformed_metadata_cannot_upgrade_capabilities(raw: object) -> None:
    assert parse_model_input_modalities(raw) == {}


def test_metadata_limits_count_ids_and_encoded_bytes() -> None:
    at_limit = {"x" * 256: ["text"], **{f"m-{i}": ["image"] for i in range(255)}}
    assert len(parse_model_input_modalities(json.dumps(at_limit))) == 256
    assert parse_model_input_modalities(json.dumps({**at_limit, "extra": ["text"]})) == {}
    raw = json.dumps({"model": ["text"]})
    padded = raw + " " * (65536 - len(raw))
    assert parse_model_input_modalities(padded) == {"model": ("text",)}
    assert parse_model_input_modalities(padded + " ") == {}
    multibyte = json.dumps({"\u00e9" * 200: ["text"]}, ensure_ascii=False) + " " * 65200
    assert len(multibyte) < 65536 < len(multibyte.encode("utf-8"))
    assert parse_model_input_modalities(multibyte) == {}


def test_catalog_metadata_matches_only_exact_id_or_wire_model() -> None:
    options = [{"id": "custom", "model": "system.ai.glm-5-3", "input_modalities": ["text"]}]
    assert model_input_modalities(options, "custom") == ("text",)
    assert model_input_modalities(options, "system.ai.glm-5-3") == ("text",)
    for model in (None, "glm-5-3", "SYSTEM.AI.GLM-5-3", "system.ai.glm-5-3[1m]", "unknown"):
        assert model_input_modalities(options, model) is None


@pytest.mark.parametrize("second", [["text", "image"], None, ["unsupported"]])
def test_conflicting_or_unknown_duplicate_catalog_rows_stay_unknown(second: object) -> None:
    options = [
        {"id": "alias", "model": "model", "input_modalities": ["text"]},
        {"id": "other", "model": "model", "input_modalities": second},
    ]
    assert model_input_modalities(options, "model") is None
    assert model_input_modalities(model_modality_options(options), "model") is None


def test_matching_duplicate_rows_normalize_modality_order() -> None:
    options = [
        {"id": "alias", "model": "model", "input_modalities": ["text", "image"]},
        {"id": "other", "model": "model", "input_modalities": ["image", "text"]},
    ]
    assert model_input_modalities(options, "model") == ("image", "text")


def test_modality_options_preserve_unknown_identity_rows_and_drop_unrelated_fields() -> None:
    rows = [
        {"id": "alias", "model": "model", "input_modalities": ["text"], "secret": "omit"},
        {"id": "image-model", "input_modalities": ["image", "text"]},
        {"id": "unknown"},
        {"id": "invalid", "input_modalities": ["audio"]},
        {"id": " padded", "input_modalities": ["image"]},
        {"model": "x" * 257, "input_modalities": ["image"]},
        {"input_modalities": ["image"]},
        None,
    ]
    assert model_modality_options(rows) == [
        {"id": "alias", "model": "model", "input_modalities": ["text"]},
        {"id": "image-model", "input_modalities": ["image", "text"]},
        {"id": "unknown"},
        {"id": "invalid"},
    ]
    assert model_modality_options([rows[0]] * 257) == []
