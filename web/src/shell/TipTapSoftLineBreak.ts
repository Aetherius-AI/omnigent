// Soft line breaks for the markdown editor.
//
// CommonMark renders a single newline inside a paragraph as a space, which is
// how the Preview pane reflows hard-wrapped prose. @tiptap/markdown copies
// marked's text tokens verbatim, so the newline reached the editor's text
// nodes, and ProseMirror's `white-space: break-spaces` then put every source
// line on its own row — an 80-column file used a quarter of a wide viewer.
//
// This mark parses each soft break into one space that carries the mark. The
// editor lays the paragraph out like Preview, and the serialiser patch in
// tiptapMarkdownPatches.ts writes the marked space back as a newline, so a
// save keeps the file's original wrapping instead of joining its lines.

import { Mark, mergeAttributes, type JSONContent } from "@tiptap/core";

export const SOFT_LINE_BREAK_MARK = "softLineBreak";

/** True for a parsed text node that stands in for a soft line break. */
export function isSoftLineBreakText(node: JSONContent | undefined): boolean {
  return (
    node?.type === "text" &&
    (node.marks ?? []).some(
      (mark) => (typeof mark === "string" ? mark : mark.type) === SOFT_LINE_BREAK_MARK,
    )
  );
}

export const SoftLineBreak = Mark.create({
  name: SOFT_LINE_BREAK_MARK,
  // The mark only ever covers the space the parser created; text typed next
  // to it must stay plain so it serialises as text, not as another newline.
  inclusive: false,
  keepOnSplit: false,

  renderHTML({ HTMLAttributes }) {
    return ["span", mergeAttributes(HTMLAttributes, { "data-soft-line-break": "" }), 0];
  },

  // marked keeps newlines inside its inline text tokens. Claiming them as a
  // token of their own makes the lexer end the text token before the newline.
  markdownTokenizer: {
    name: SOFT_LINE_BREAK_MARK,
    level: "inline",
    // Stop the text token before any trailing spaces or backslash as well, so
    // marked's own `br` rule still sees a hard break (`  \n`, `\\n`) whole.
    start: (src) => {
      const newline = src.indexOf("\n");
      if (newline === -1) return -1;
      let start = newline;
      while (start > 0 && src[start - 1] === " ") start -= 1;
      if (start > 0 && src[start - 1] === "\\") start -= 1;
      return start;
    },
    tokenize: (src) =>
      src.startsWith("\n") ? { type: SOFT_LINE_BREAK_MARK, raw: "\n" } : undefined,
  },
  parseMarkdown: (_token, helpers) =>
    helpers.applyMark(SOFT_LINE_BREAK_MARK, [helpers.createTextNode(" ")]),
  // No delimiters; the serialiser patch turns the marked space itself into
  // the newline.
  renderMarkdown: (node, h) => h.renderChildren(node),
});
