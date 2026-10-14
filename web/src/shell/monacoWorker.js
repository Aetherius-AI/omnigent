import { initialize } from "monaco-editor/esm/vs/base/common/worker/webWorkerBootstrap.js";
import { EditorWorker } from "monaco-editor/esm/vs/editor/common/services/editorWebWorker.js";

// Keep expensive refinement off the worker that serves newly opened files.
class RefiningEditorWorker extends EditorWorker {
  refinements = new Set();
  nextRefinementId = 0;

  cancelRefinements(uri) {
    for (const task of this.refinements) {
      if (task.original === uri || task.modified === uri) task.finish(null);
    }
  }

  $acceptRemovedModel(uri) {
    this.cancelRefinements(uri);
    super.$acceptRemovedModel(uri);
  }

  $acceptModelChanged(uri, event) {
    this.cancelRefinements(uri);
    super.$acceptModelChanged(uri, event);
  }

  async $computeDiff(original, modified, options, algorithm) {
    this.cancelRefinements(original);
    if (options.maxComputationTimeMs <= 1000) {
      return super.$computeDiff(original, modified, options, algorithm);
    }
    const before = this.getModels().find((model) => model.uri.toString() === original);
    const after = this.getModels().find((model) => model.uri.toString() === modified);
    if (!before || !after) return null;
    // This complete, coarse result is also safe to return on worker failure/cancellation.
    const fallback = await super.$computeDiff(
      original,
      modified,
      { ...options, maxComputationTimeMs: 1 },
      algorithm,
    );
    return new Promise((resolve) => {
      const id = ++this.nextRefinementId;
      let finished = false;
      const task = {
        id,
        original,
        modified,
        finish: (result) => {
          if (finished) return;
          finished = true;
          clearTimeout(watchdog);
          self.postMessage({ type: "cancel-refinement", id });
          this.refinements.delete(task);
          resolve(result ?? fallback);
        },
      };
      // Also bound worker lifetime if loading or computation stalls.
      const watchdog = setTimeout(() => task.finish(null), options.maxComputationTimeMs + 2000);
      this.refinements.add(task);
      self.postMessage({
        type: "refine-diff",
        id,
        input: {
          before: before.getLinesContent(),
          after: after.getLinesContent(),
          options,
          algorithm,
        },
      });
    });
  }
}

self.onmessage = () => {
  const editor = new RefiningEditorWorker();
  initialize(() => editor);
  const handleMonacoMessage = self.onmessage;
  self.onmessage = (event) => {
    if (event.data?.type === "refined-diff") {
      for (const task of editor.refinements) {
        if (task.id === event.data.id) task.finish(event.data.result);
      }
    } else {
      handleMonacoMessage(event);
    }
  };
};
