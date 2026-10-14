// Soft line break: a newline inside a paragraph's markdown source. @tiptap/markdown
// keeps it as "\n" and ProseMirror's break-spaces CSS draws it as a line break, so
// hard-wrapped prose read as short lines in Edit mode. This inline leaf renders as a
// collapsible space instead; tiptapMarkdownPatches.ts splits and rejoins it around "\n".

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
  // TipTap's renderText does not populate; both keep comment anchors and copied
  // text byte-identical to the raw file.
  extendNodeSchema(extension) {
    return extension.name === SOFT_BREAK_NODE ? { leafText: () => "\n" } : {};
  },

  // Fallback only: the serialiser patch rejoins soft breaks into text before
  // rendering so marks spanning a break are not closed and reopened around it.
  renderMarkdown: () => "\n",
});
