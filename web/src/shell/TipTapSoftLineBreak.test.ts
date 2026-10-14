/**
 * Soft line breaks in the markdown editor.
 *
 * A paragraph hard-wrapped in the source must flow like the rendered Preview
 * (no newline left in the editor's text), and serialising must write the
 * original line breaks back so a save never rewrites the file's wrapping.
 * Uses a real TipTap Editor with the viewer's markdown extension stack.
 */

import type * as UseFileContentModule from "@/hooks/useFileContent";

import { afterEach, describe, expect, it, vi } from "vitest";
import { Editor } from "@tiptap/core";
import { StarterKit } from "@tiptap/starter-kit";
import { Table, TableCell, TableHeader, TableRow } from "@tiptap/extension-table";
import { ListItem, TaskItem, TaskList } from "@tiptap/extension-list";
import { Markdown } from "@tiptap/markdown";
import { createWorkspaceImageExtension, ImageAwareLink } from "./TipTapWorkspaceImage";
import { GitHubAlertBlockquote } from "./TipTapGitHubAlert";
import { HtmlPassthrough } from "./TipTapHtmlPassthrough";
import { SOFT_LINE_BREAK_MARK, SoftLineBreak } from "./TipTapSoftLineBreak";
import {
  installMarkdownParserPatch,
  installMarkdownSerializerPatch,
} from "./tiptapMarkdownPatches";

vi.mock("@/hooks/useFileContent", async (importOriginal) => {
  const actual = await importOriginal<typeof UseFileContentModule>();
  return { ...actual, fetchFileContent: vi.fn().mockResolvedValue(undefined) };
});

installMarkdownSerializerPatch();
installMarkdownParserPatch();

let editor: Editor | null = null;
afterEach(() => {
  editor?.destroy();
  editor = null;
});

function makeEditor(markdown: string): Editor {
  return new Editor({
    element: document.createElement("div"),
    extensions: [
      StarterKit.configure({ link: false, blockquote: false, listItem: false }),
      ListItem.extend({ content: "block+" }),
      TaskList,
      TaskItem.configure({ nested: true }),
      Table.configure({ resizable: true }),
      TableRow,
      TableCell,
      TableHeader,
      ImageAwareLink.configure({ openOnClick: false, autolink: false }),
      GitHubAlertBlockquote,
      HtmlPassthrough,
      SoftLineBreak,
      Markdown,
      createWorkspaceImageExtension("conv_test", "notes.md"),
    ],
    content: markdown,
    contentType: "markdown",
  });
}

/** Parse + serialise, normalising only the trailing newline. */
function roundTrip(markdown: string): string {
  editor = makeEditor(markdown);
  const out = editor.getMarkdown().trim();
  editor.destroy();
  editor = null;
  return out;
}

/** Text of every text node carrying the soft-line-break mark, in document order. */
function softBreakTexts(ed: Editor): string[] {
  const texts: string[] = [];
  ed.state.doc.descendants((node) => {
    if (node.isText && node.marks.some((mark) => mark.type.name === SOFT_LINE_BREAK_MARK)) {
      texts.push(node.text ?? "");
    }
  });
  return texts;
}

const WRAPPED_PARAGRAPH = [
  "The rollout plan covers the three regional clusters that still run the",
  "previous release. Each cluster is upgraded during its own maintenance",
  "window.",
].join("\n");

describe("SoftLineBreak parsing", () => {
  it("flows a hard-wrapped paragraph as one run of text", () => {
    editor = makeEditor(`# Report\n\n${WRAPPED_PARAGRAPH}`);
    const paragraph = editor.state.doc.child(1);
    expect(paragraph.type.name).toBe("paragraph");
    expect(paragraph.textContent).toBe(WRAPPED_PARAGRAPH.replace(/\n/g, " "));
    // Nothing is left for `white-space: break-spaces` to render as a line break.
    expect(editor.view.dom.querySelector("p")?.textContent).not.toContain("\n");
  });

  it("marks exactly the spaces that replaced the source newlines", () => {
    editor = makeEditor(WRAPPED_PARAGRAPH);
    expect(softBreakTexts(editor)).toEqual([" ", " "]);
  });

  it("leaves code block newlines alone", () => {
    editor = makeEditor("```\nline one\nline two\n```");
    const code = editor.state.doc.child(0);
    expect(code.type.name).toBe("codeBlock");
    expect(code.textContent).toBe("line one\nline two");
    expect(softBreakTexts(editor)).toEqual([]);
  });

  it.each(["first  \nsecond", "first\\\nsecond"])("keeps a hard break a hard break: %j", (md) => {
    editor = makeEditor(md);
    const paragraph = editor.state.doc.child(0);
    expect(paragraph.child(1).type.name).toBe("hardBreak");
    expect(softBreakTexts(editor)).toEqual([]);
  });
});

describe("SoftLineBreak serialisation", () => {
  it.each([
    WRAPPED_PARAGRAPH,
    "**bold that wraps\nonto a second line** and more",
    "[link text that\nwraps](https://example.com/docs)",
    "- item one continues\non the next line\n- item two",
    "> quoted prose\n> on two lines",
    "> [!NOTE]\n> Teammates need to reach the server.\n> A local server is network-only.",
  ])("writes the original line breaks back: %j", (markdown) => {
    expect(roundTrip(markdown)).toBe(markdown);
  });

  it("serialises text typed on either side of a break as plain text", () => {
    editor = makeEditor("first line\nsecond line");
    // The paragraph opens at position 0, so text offset n is position n + 1.
    const afterBreak = 1 + "first line ".length;
    editor.view.dispatch(editor.state.tr.insertText("X", afterBreak));
    editor.view.dispatch(editor.state.tr.insertText("Y", afterBreak - 1));
    expect(editor.getMarkdown().trim()).toBe("first lineY\nXsecond line");
  });

  it("keeps a single break when the word between two breaks is deleted", () => {
    editor = makeEditor("alpha\nbeta\ngamma");
    const betaFrom = 1 + "alpha ".length;
    editor.view.dispatch(editor.state.tr.delete(betaFrom, betaFrom + "beta".length));
    expect(editor.getMarkdown().trim()).toBe("alpha\ngamma");
  });
});
