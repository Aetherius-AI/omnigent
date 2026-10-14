"""E2E: the markdown rich-text editor reflows hard-wrapped prose to the viewer width.

Counterpart to ``test_markdown_rich_rendering.py`` (construct rendering): a
paragraph whose source is hard-wrapped at 80 columns reads as one flowing block
in Preview, and Edit mode must lay it out across the same width instead of
rendering one short visual line per source line. Seeded via the filesystem PUT
endpoint (no agent run).
"""

from __future__ import annotations

import contextlib
import re
import textwrap
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

from tests.e2e_ui.conftest import open_right_rail, switch_markdown_view_mode

_MARKDOWN_FILE_PATH = "design_wrapped.md"

_INTRO_TEXT = (
    "This design note describes how the workspace file viewer renders Markdown "
    "documents in its three modes. The rendered preview reflows every paragraph to "
    "the full width of the viewer, while the editing surface must keep the text "
    "selectable so reviewers can anchor comments to exact phrases in the file. Both "
    "surfaces read the same source bytes, so any difference in how far a line of "
    "prose extends across the panel comes from the way each surface lays the text "
    "out rather than from the document itself."
)

# Prose hard-wrapped at 80 columns, the way editors and agents commonly write it.
_INTRO_LINES = textwrap.fill(_INTRO_TEXT, width=80).split("\n")
_MARKDOWN_CONTENT = f"""\
# Wrapped Design Document

{chr(10).join(_INTRO_LINES)}

## Goals

A second paragraph that stays on one line.
"""

# Measure how far a paragraph's text extends across its box; returns an error
# object instead of numbers when the paragraph is missing or hidden.
_PARAGRAPH_LAYOUT_JS = """
(root, prefix) => {
  const paragraph = [...root.querySelectorAll("p")]
    .find((el) => el.textContent.startsWith(prefix));
  if (!paragraph) return {error: `no paragraph starting with ${JSON.stringify(prefix)}`};
  const range = document.createRange();
  range.selectNodeContents(paragraph);
  const lines = [];
  for (const rect of range.getClientRects()) {
    if (rect.width === 0 || rect.height === 0) continue;
    const line = lines.find((l) => Math.abs(l.top - rect.top) < 3);
    if (line) {
      line.left = Math.min(line.left, rect.left);
      line.right = Math.max(line.right, rect.right);
    } else {
      lines.push({top: rect.top, left: rect.left, right: rect.right});
    }
  }
  if (lines.length === 0) return {error: "paragraph has no laid-out text (hidden?)"};
  const box = paragraph.getBoundingClientRect();
  const textRight = Math.max(...lines.map((l) => l.right));
  return {
    lineCount: lines.length,
    paragraphWidth: box.width,
    fillRatio: (textRight - box.left) / box.width,
  };
}
"""


@pytest.fixture
def seeded_wrapped_markdown_session(
    seeded_session: tuple[str, str],
) -> Iterator[tuple[str, str]]:
    """Seed the hard-wrapped markdown file and yield (base_url, session_id)."""
    base_url, session_id = seeded_session
    file_url = (
        f"{base_url}/v1/sessions/{session_id}"
        f"/resources/environments/default/filesystem/{_MARKDOWN_FILE_PATH}"
    )
    resp = httpx.put(
        file_url, json={"content": _MARKDOWN_CONTENT, "encoding": "utf-8"}, timeout=10.0
    )
    resp.raise_for_status()
    try:
        yield (base_url, session_id)
    finally:
        # Deleting the session keeps its workspace files, so remove the seed here.
        # A cleanup failure must not mask the test's own result.
        with contextlib.suppress(httpx.HTTPError):
            httpx.delete(file_url, timeout=10.0)


# Select from the last word before the first soft break to the first word after it
# and record what the next copy puts on the clipboard as plain text.
_SELECT_ACROSS_SOFT_BREAK_JS = """
(editor, [lastWord, firstWord]) => {
  const span = editor.querySelector("span.md-soft-break");
  const before = span && span.previousSibling;
  const after = span && span.nextSibling;
  const isText = (node) => node && node.nodeType === Node.TEXT_NODE;
  if (!isText(before) || !isText(after)) {
    return "first soft break is not between two text nodes";
  }
  const range = document.createRange();
  range.setStart(before, before.textContent.length - lastWord.length);
  range.setEnd(after, firstWord.length);
  const selection = window.getSelection();
  selection.removeAllRanges();
  selection.addRange(range);
  window.__copiedText = null;
  document.addEventListener(
    "copy",
    (event) => { window.__copiedText = event.clipboardData.getData("text/plain"); },
    { once: true },
  );
  return "ok";
}
"""


def _paragraph_layout(file_viewer: Locator, root_selector: str) -> dict[str, float]:
    layout = file_viewer.locator(root_selector).evaluate(_PARAGRAPH_LAYOUT_JS, "This design note")
    assert "error" not in layout, layout["error"]
    return layout


def _open_file_full_screen(page: Page, base_url: str, session_id: str) -> Locator:
    page.goto(f"{base_url}/c/{session_id}?view=explore")
    open_right_rail(page)
    file_button = page.get_by_role(
        "button", name=re.compile(rf"^{re.escape(_MARKDOWN_FILE_PATH)}\b")
    )
    expect(file_button).to_be_visible(timeout=30_000)
    file_button.click()
    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer).to_be_visible()
    expect(
        page.get_by_role("button", name=f"Close {_MARKDOWN_FILE_PATH}", exact=True).first
    ).to_be_visible()
    page.get_by_role("button", name="Full screen", exact=True).click()
    return file_viewer


@pytest.mark.browser_context_args(viewport={"width": 1600, "height": 900})
def test_edit_mode_reflows_hard_wrapped_paragraph_to_viewer_width(
    request: pytest.FixtureRequest,
    seeded_wrapped_markdown_session: tuple[str, str],
) -> None:
    """A hard-wrapped paragraph spans the editor width in Edit mode as it does in Preview."""
    base_url, session_id = seeded_wrapped_markdown_session
    page: Page = request.getfixturevalue("page")
    file_viewer = _open_file_full_screen(page, base_url, session_id)

    switch_markdown_view_mode(page, file_viewer, "Preview")
    preview_paragraph = file_viewer.locator(".markdown-preview p").filter(
        has_text="This design note"
    )
    expect(preview_paragraph).to_be_visible(timeout=10_000)
    preview = _paragraph_layout(file_viewer, ".markdown-preview")
    assert preview["fillRatio"] > 0.9, preview

    switch_markdown_view_mode(page, file_viewer, "Edit")
    editor = file_viewer.locator("[contenteditable='true']")
    expect(editor).to_be_visible(timeout=10_000)
    expect(editor.locator("h1")).to_contain_text("Wrapped Design Document")
    edit = _paragraph_layout(file_viewer, ".ProseMirror")
    # Wide enough that eighty-column lines cannot fill the box by themselves.
    assert edit["paragraphWidth"] > 900, edit
    assert edit["fillRatio"] > 0.9, (
        f"Edit mode text spans only {edit['fillRatio']:.0%} of its {edit['paragraphWidth']:.0f}px "
        f"paragraph box over {edit['lineCount']} lines; Preview spans "
        f"{preview['fillRatio']:.0%} over {preview['lineCount']} lines"
    )

    # Copying across the reflowed line still yields the source newline, not a space.
    last_word = _INTRO_LINES[0].rsplit(" ", 1)[1]
    first_word = _INTRO_LINES[1].split(" ", 1)[0]
    editor.locator("h1").click()
    assert editor.evaluate(_SELECT_ACROSS_SOFT_BREAK_JS, [last_word, first_word]) == "ok"
    page.keyboard.press("ControlOrMeta+c")
    assert page.evaluate("window.__copiedText") == f"{last_word}\n{first_word}"
