import { afterEach, expect, it, vi } from "vitest";

interface TestEditorWorker {
  $computeDiff: (
    original: string,
    modified: string,
    options: { maxComputationTimeMs: number },
    algorithm: string,
  ) => Promise<unknown>;
  $acceptRemovedModel: (uri: string) => void;
  $acceptModelChanged: (uri: string, event: object) => void;
}

const h = vi.hoisted(() => ({
  factory: null as null | (() => TestEditorWorker),
  compute: vi.fn(),
}));
vi.mock("monaco-editor/esm/vs/base/common/worker/webWorkerBootstrap.js", () => ({
  initialize: (factory: () => TestEditorWorker) => {
    h.factory = factory;
    self.onmessage = vi.fn();
  },
}));
vi.mock("monaco-editor/esm/vs/editor/common/services/editorWebWorker.js", () => ({
  EditorWorker: class {
    getModels() {
      return ["old", "current", "other-old"].map((uri) => ({
        uri,
        getLinesContent: () => ["content"],
      }));
    }
    $computeDiff(...args: unknown[]) {
      return h.compute(...args);
    }
    $acceptRemovedModel() {}
    $acceptModelChanged() {}
  },
}));

async function setup() {
  vi.resetModules();
  vi.useFakeTimers();
  const fallback = { quitEarly: true, changes: ["coarse"] };
  h.compute.mockReset().mockResolvedValue(fallback);
  const post = vi.fn();
  vi.stubGlobal("postMessage", post);
  // The worker entry is JS, matching Monaco's untyped worker implementation.
  // @ts-expect-error Worker entry has no TypeScript declarations.
  await import("./monacoWorker.js");
  self.onmessage?.(new MessageEvent("message"));
  const editor = h.factory!();
  return { editor, post, fallback };
}
afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
  self.onmessage = null;
});

it("serves initial comparisons while refinement is busy and terminates it on navigation", async () => {
  const { editor, post, fallback } = await setup();
  const refined = editor.$computeDiff(
    "old",
    "current",
    { maxComputationTimeMs: 10000 },
    "advanced",
  );
  await Promise.resolve();
  expect(post).toHaveBeenCalledWith(expect.objectContaining({ type: "refine-diff" }));
  const initial = editor.$computeDiff(
    "other-old",
    "other-current",
    { maxComputationTimeMs: 1000 },
    "advanced",
  );
  expect(await initial).toBe(fallback);
  editor.$acceptRemovedModel("current");
  expect(await refined).toBe(fallback);
  expect(post).toHaveBeenLastCalledWith({ type: "cancel-refinement", id: 1 });
});

it("returns the detailed result and releases its worker", async () => {
  const { editor, post } = await setup();
  const pending = editor.$computeDiff(
    "old",
    "current",
    { maxComputationTimeMs: 10000 },
    "advanced",
  );
  await Promise.resolve();
  const detailed = { quitEarly: false, changes: ["detailed"] };
  self.onmessage?.(
    new MessageEvent("message", { data: { type: "refined-diff", id: 1, result: detailed } }),
  );
  expect(await pending).toBe(detailed);
  expect(post).toHaveBeenLastCalledWith({ type: "cancel-refinement", id: 1 });
});

it.each(["error", "deadline", "content-change"])(
  "keeps a complete coarse diff after %s",
  async (reason) => {
    const { editor, post, fallback } = await setup();
    const pending = editor.$computeDiff(
      "old",
      "current",
      { maxComputationTimeMs: 10000 },
      "advanced",
    );
    await Promise.resolve();
    if (reason === "error")
      self.onmessage?.(
        new MessageEvent("message", { data: { type: "refined-diff", id: 1, result: null } }),
      );
    if (reason === "deadline") vi.runAllTimers();
    if (reason === "content-change") editor.$acceptModelChanged("current", {});
    expect(await pending).toBe(fallback);
    expect(post).toHaveBeenLastCalledWith({ type: "cancel-refinement", id: 1 });
  },
);

it("cancels a superseded comparison that shares only the modified model", async () => {
  const { editor, post, fallback } = await setup();
  const pending = editor.$computeDiff(
    "old",
    "current",
    { maxComputationTimeMs: 10000 },
    "advanced",
  );
  await Promise.resolve();
  await editor.$computeDiff("other-old", "current", { maxComputationTimeMs: 1000 }, "advanced");
  expect(await pending).toBe(fallback);
  expect(post).toHaveBeenLastCalledWith({ type: "cancel-refinement", id: 1 });
});
