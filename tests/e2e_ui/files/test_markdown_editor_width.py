"""E2E: a hard-wrapped Markdown paragraph fills the rich-text editor's width.

The rendered Preview reflows an 80-column paragraph across the whole file
viewer; the Edit mode (the surface where commenting works) should lay the same
paragraph out just as wide instead of keeping each source line on its own row.
Seeded via the filesystem PUT endpoint (no agent run).
"""

from __future__ import annotations

import json
import os
import re
import textwrap
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

from tests.e2e_ui.conftest import open_right_rail, switch_markdown_view_mode

_MARKDOWN_FILE_PATH = "report_wrapped.md"

_FIRST_PARAGRAPH_START = "The rollout plan covers"
_CONTROL_PARAGRAPH_START = "Control paragraph kept on one source line"

# Prose hard-wrapped at 80 columns, the way editors and agents usually write
# Markdown; the control paragraph stays on one source line.
_WRAPPED_PARAGRAPHS = [
    textwrap.fill(" ".join(text.split()), width=80)
    for text in (
        """
        The rollout plan covers the three regional clusters that still run the previous
        release. Each cluster is upgraded during its own maintenance window, and the
        traffic manager drains connections from a node before the package is replaced.
        A canary group of ten percent of the fleet receives the build two days early so
        that the observability dashboards can confirm error budgets before the rest of
        the fleet follows. Rollback is a single command that restores the prior image.
        """,
        """
        The migration of the metadata store runs in parallel with the cluster upgrades
        because the two tasks share no dependencies. Schema changes are applied through
        the usual expand-and-contract pattern: new columns are added first, the writers
        start populating both representations, and the old columns are dropped only
        after the readers have been switched over and the comparison job reports zero
        divergent rows for a full week of production traffic.
        """,
        """
        The largest risk is the vendor appliance in the smallest region, which cannot
        be upgraded without a maintenance contract renewal that is still waiting for a
        signature. If the renewal slips past the second window, that region keeps the
        previous release for one more cycle and the compatibility shim stays enabled.
        """,
    )
]

_CONTROL_PARAGRAPH = (
    f"{_CONTROL_PARAGRAPH_START}: it carries roughly the same amount of text as the"
    " wrapped paragraphs above but has no embedded line breaks, so the editor has no"
    " reason to narrow it, and it serves as the yardstick for the paragraphs that do."
)

_MARKDOWN_CONTENT = f"""\
# Quarterly rollout report

{_WRAPPED_PARAGRAPHS[0]}

## Timeline

{_WRAPPED_PARAGRAPHS[1]}

### Risks

{_WRAPPED_PARAGRAPHS[2]}

---

{_CONTROL_PARAGRAPH}
"""

# Fraction of the paragraph's content width that its widest text line must
# reach to count as filling the surface.
_FILLED_FRACTION = 0.9


@pytest.fixture
def seeded_wrapped_markdown_session(
    seeded_session: tuple[str, str],
) -> Iterator[tuple[str, str]]:
    base_url, session_id = seeded_session
    file_url = (
        f"{base_url}/v1/sessions/{session_id}"
        f"/resources/environments/default/filesystem/{_MARKDOWN_FILE_PATH}"
    )
    resp = httpx.put(
        file_url,
        json={"content": _MARKDOWN_CONTENT, "encoding": "utf-8"},
        timeout=10.0,
    )
    resp.raise_for_status()
    try:
        yield (base_url, session_id)
    finally:
        # Deleting the session keeps its workspace files, so remove the seed here.
        httpx.delete(file_url, timeout=10.0)


def _measure_paragraph(paragraph: Locator) -> dict[str, object]:
    """Line boxes of ``paragraph`` relative to its own content box."""
    return paragraph.evaluate(
        """(el) => {
          const range = document.createRange();
          range.selectNodeContents(el);
          const rects = [...range.getClientRects()].filter((r) => r.width > 0);
          const style = getComputedStyle(el);
          const contentLeft = el.getBoundingClientRect().left + parseFloat(style.paddingLeft);
          const contentWidth =
            el.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
          const textRight = Math.max(...rects.map((r) => r.right));
          const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
          let newlineTextNodes = 0;
          while (walker.nextNode()) {
            if (walker.currentNode.nodeValue.includes("\\n")) newlineTextNodes += 1;
          }
          return {
            lines: new Set(rects.map((r) => Math.round(r.top))).size,
            text_width: Math.round(textRight - contentLeft),
            content_width: Math.round(contentWidth),
            fraction: Math.round(((textRight - contentLeft) / contentWidth) * 1000) / 1000,
            br_count: el.querySelectorAll("br").length,
            newline_text_nodes: newlineTextNodes,
            white_space: style.whiteSpace,
          };
        }"""
    )


def _hold_for_recording(page: Page, name: str) -> None:
    """When the suite is filming, keep each state on screen and save a frame."""
    record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
    if not record_dir:
        return
    page.wait_for_timeout(2_000)
    page.screenshot(path=str(Path(record_dir) / f"{name}.png"))


@pytest.mark.browser_context_args(
    viewport={"width": 1600, "height": 900}, record_video_size={"width": 1600, "height": 900}
)
def test_wrapped_paragraph_fills_editor_width_like_preview(
    request: pytest.FixtureRequest,
    seeded_wrapped_markdown_session: tuple[str, str],
) -> None:
    """Edit mode lays an 80-column paragraph out as wide as Preview does."""
    base_url, session_id = seeded_wrapped_markdown_session
    page: Page = request.getfixturevalue("page")
    page.goto(f"{base_url}/c/{session_id}")

    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name=re.compile("^Files")).click()
    file_row = rail.get_by_role("button", name=re.compile(re.escape(_MARKDOWN_FILE_PATH))).filter(
        has_text=_MARKDOWN_FILE_PATH
    )
    expect(file_row).to_be_visible(timeout=30_000)
    file_row.click()

    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer).to_be_visible()
    editor = file_viewer.locator("[contenteditable='true']")
    editor_paragraph = editor.locator("p").filter(has_text=_FIRST_PARAGRAPH_START)
    expect(editor_paragraph).to_be_visible(timeout=10_000)
    measurements: dict[str, object] = {
        "session_id": session_id,
        "viewport": page.viewport_size,
        "edit_default_width": _measure_paragraph(editor_paragraph),
    }
    _hold_for_recording(page, "edit-default-width")

    rail.get_by_role("button", name="Full screen", exact=True).click()
    switch_markdown_view_mode(page, file_viewer, "Preview")
    preview_paragraph = file_viewer.locator(".markdown-preview p").filter(
        has_text=_FIRST_PARAGRAPH_START
    )
    expect(preview_paragraph).to_be_visible(timeout=10_000)
    measurements["preview_full_screen"] = preview = _measure_paragraph(preview_paragraph)
    _hold_for_recording(page, "preview-full-screen")

    switch_markdown_view_mode(page, file_viewer, "Edit")
    editor_paragraph = editor.locator("p").filter(has_text=_FIRST_PARAGRAPH_START)
    expect(editor_paragraph).to_be_visible(timeout=10_000)
    measurements["edit_full_screen"] = edit = _measure_paragraph(editor_paragraph)
    measurements["edit_control_full_screen"] = _measure_paragraph(
        editor.locator("p").filter(has_text=_CONTROL_PARAGRAPH_START)
    )
    _hold_for_recording(page, "edit-full-screen")
    print("markdown editor width measurements:", json.dumps(measurements))

    assert preview["fraction"] >= _FILLED_FRACTION, measurements
    assert edit["fraction"] >= _FILLED_FRACTION, (
        f"Edit mode uses {edit['fraction']:.0%} of the editor width over {edit['lines']} lines"
        f" while Preview uses {preview['fraction']:.0%}: {json.dumps(measurements)}"
    )
