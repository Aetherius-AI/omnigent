"""Browser contract: a fresh image refused by a text-only Claude model keeps the draft.

The server answers the refused send with the validation error
``omnigent.models.input_modalities.image_model_refusal`` and retains the
message as an ``undelivered`` row with an "Image was not sent" error item. The
backend is mocked here, so this pins only the web side of that contract: the
composer keeps the typed text and the image chip, the refusal reason is shown
in the chat, and the retained row and banner render after a reload.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Page, Route, expect

from omnigent.models.input_modalities import IMAGE_INPUT_REFUSED_CODE, image_model_refusal
from tests.browser_ui.chat.session_contract import ChatSessionContract, model_option

_COMPOSER = "Send a message…"
_USER = '[data-testid="message-bubble"][data-role="user"]'
_IMAGE_NAME = "fresh-screenshot.png"
_PROMPT = "What is in this screenshot?"
_TEXT_MODEL = "managed.text-model"
_IMAGE_MODEL = "managed.image-model"
_BANNER_TITLE = "Image was not sent"
_REMEDIATION = "Choose a model that supports images, then resend the complete message."
# 1x1 PNG.
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)
# Hold each visible state long enough for a recording of this journey to be read.
_HOLD_MS = 2_000


@pytest.fixture
def browser_context_args(browser_context_args: dict[str, Any]) -> dict[str, Any]:
    size = {"width": 1280, "height": 720}
    return {**browser_context_args, "viewport": size, "record_video_size": size}


def test_text_only_model_image_refusal_keeps_the_draft_and_shows_guidance(
    page: Page, chat_session_contract: ChatSessionContract, tmp_path: Path
) -> None:
    chat = chat_session_contract
    chat.set_catalog(
        harness="claude-native",
        models=[
            {
                **model_option(_TEXT_MODEL, display_name="Text model", is_default=True),
                "input_modalities": ["text"],
            },
            {
                **model_option(_IMAGE_MODEL, display_name="Image model"),
                "input_modalities": ["text", "image"],
            },
        ],
        selected_model=_TEXT_MODEL,
    )
    chat.reject_uploads = False
    session_api = f"{chat.base_url}/v1/sessions/{chat.session_id}"
    chat.contract.route(
        re.compile(rf"^{re.escape(session_api)}/resources/files/[^/]+/content(?:\?.*)?$"),
        lambda route: route.fulfill(status=200, content_type="image/png", body=_PNG),
    )

    posts: list[dict[str, Any]] = []
    refusal = image_model_refusal(_TEXT_MODEL)

    def refuse_event(route: Route) -> None:
        if route.request.method != "POST":
            route.fallback()
            return
        body = json.loads(route.request.post_data or "{}")
        posts.append(body)
        # The server retains the refused input before answering, under a fresh
        # item id so the client's send id stays free for an explicit resend.
        chat.emit(
            {
                "event": "session.input.consumed",
                "data": {
                    "type": "session.input.consumed",
                    "data": {
                        "item_id": "refused-image-message",
                        "type": "message",
                        "cleared_pending_id": None,
                        "data": {
                            "role": "user",
                            "content": body["data"]["content"],
                            "user_authored": True,
                            "delivery_status": "undelivered",
                        },
                    },
                },
            }
        )
        route.fulfill(
            status=400,
            content_type="application/json",
            body=json.dumps({"error": {"code": "invalid_input", "message": refusal}}),
        )

    chat.contract.route(re.compile(rf"^{re.escape(session_api)}/events(?:\?.*)?$"), refuse_event)

    sample = tmp_path / _IMAGE_NAME
    sample.write_bytes(_PNG)

    page.goto(chat.url)
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible(timeout=30_000)
    chat.wait_for_stream()

    page.wait_for_timeout(_HOLD_MS)

    page.locator('input[type="file"][accept*="image/"]').set_input_files(str(sample))
    chip = page.get_by_role("button", name=f"Remove {_IMAGE_NAME}")
    expect(chip).to_be_visible(timeout=10_000)
    page.wait_for_timeout(_HOLD_MS)
    composer.fill(_PROMPT)
    page.wait_for_timeout(_HOLD_MS)
    page.get_by_role("button", name="Send", exact=True).click()

    # The refusal reason is shown and the draft comes back, image included.
    pill = page.get_by_test_id("error-pill")
    expect(pill).to_be_visible(timeout=30_000)
    expect(composer).to_have_value(_PROMPT, timeout=10_000)
    expect(chip).to_be_visible(timeout=10_000)
    expect(page.locator(_USER)).to_have_count(1)
    expect(page.locator(_USER).get_by_text(_PROMPT, exact=False)).to_be_visible()
    page.wait_for_timeout(_HOLD_MS)
    pill.get_by_test_id("error-headline").click()
    expect(page.get_by_text("supports text only", exact=False)).to_be_visible()
    expect(page.get_by_text("Choose a model that supports images", exact=False)).to_be_visible()
    page.wait_for_timeout(_HOLD_MS * 2)

    assert len(posts) == 1, posts
    content = posts[0]["data"]["content"]
    assert {"type": "input_text", "text": _PROMPT} in content
    image_blocks = [block for block in content if block.get("type") == "input_image"]
    assert image_blocks == [
        {"type": "input_image", "file_id": "browser-upload-1", "filename": _IMAGE_NAME}
    ]

    # After a reload the retained row and its banner come from the server.
    chat.set_items(
        [
            {
                "id": "refused-image-validation",
                "response_id": "refused-turn",
                "type": "error",
                "source": "harness",
                "code": IMAGE_INPUT_REFUSED_CODE,
                "message": refusal,
                "title": _BANNER_TITLE,
                "remediation": _REMEDIATION,
            },
            {
                "id": "refused-image-message",
                "response_id": "refused-turn",
                "type": "message",
                "role": "user",
                "status": "completed",
                "content": content,
                "user_authored": True,
                "delivery_status": "undelivered",
            },
        ]
    )
    page.reload()
    expect(page.get_by_placeholder(_COMPOSER)).to_be_visible(timeout=30_000)
    expect(page.locator(_USER)).to_have_count(1)
    expect(page.locator(_USER).get_by_text(_PROMPT, exact=False)).to_be_visible()
    headline = page.get_by_test_id("error-headline")
    expect(headline).to_contain_text(_BANNER_TITLE, timeout=10_000)
    page.wait_for_timeout(_HOLD_MS)
    headline.click()
    expect(page.get_by_text(_REMEDIATION, exact=False)).to_be_visible()
    page.wait_for_timeout(_HOLD_MS * 2)
