// Soft line break: a newline inside a paragraph's markdown source.
//
// @tiptap/markdown keeps those newlines as "\n" in text nodes, and ProseMirror's
// editing CSS (white-space: break-spaces) draws each one as a line break, so a
// hard-wrapped paragraph reads as short lines in Edit mode while Preview reflows
// it. This node stands in for each soft break and renders as one collapsible
// space; the patches in tiptapMarkdownPatches.ts split "\n" into these nodes
// after parsing and join them back before serialising, so saves stay
// byte-identical. textBetween()/getText()/clipboard still read "\n", so comment
// anchors keep matching the raw file.

import { Node, mergeAttributes } from "@tiptap/core";

export const SOFT_BREAK_NODE = "softBreak";

export const SoftBreak = Node.create({
  name: SOFT_BREAK_NODE,
  inline: true,
  group: "inline",
  selectable: false,

  parseHTML() {
    return [{ tag: "span[data-soft-break]" }];
  },

  renderHTML({ HTMLAttributes }) {
    return [
      "span",
      mergeAttributes(HTMLAttributes, { "data-soft-break": "", class: "md-soft-break" }),
      " ",
    ];
  },

  renderText() {
    return "\n";
  },

  // ProseMirror's textBetween() reads leaf text from the schema spec, which
  // TipTap's renderText does not populate.
  extendNodeSchema(extension) {
    return extension.name === SOFT_BREAK_NODE ? { leafText: () => "\n" } : {};
  },

  // Fallback only: the serialiser patch rejoins soft breaks into text before
  // rendering so marks spanning a break are not closed and reopened around it.
  renderMarkdown: () => "\n",
});
