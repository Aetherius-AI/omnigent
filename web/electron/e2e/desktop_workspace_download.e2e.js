// Desktop e2e: downloading a workspace file from the Files panel when the
// deployment routes host-scoped requests by the app's slice-key header.
//
// Stack (all local, see fixtures/): mock LLM + `omnigent server` serving an SPA
// built with VITE_OMNIGENT_HOST_ROUTING=true, a real `omnigent host` with a
// session launched on it, and a routing-ingress stand-in in front of the server
// that answers keyless tunnel-dependent requests as a wrong replica would. The
// real Electron shell connects through the stand-in; a direct-to-server case
// shows the same download without the ingress.
//
// Run from web/electron after building the SPA:
//   OMNIGENT_PYTHON=../../.venv/bin/python OMNIGENT_PW_NO_SANDBOX=1 \
//     xvfb-run -a node --test e2e/desktop_workspace_download.e2e.js
// Optional: OMNIGENT_ROUTING_WEB_UI_DIST (prebuilt routing bundle),
// OMNIGENT_DESKTOP_RECORD_DIR (recordings + ingress log).

"use strict";

const { describe, it, before, after } = require("node:test");
const assert = require("node:assert/strict");
const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const {
  APP_ROOT,
  desktopDepsAvailable,
  spawnServer,
  launchDesktop,
  saveRecording,
} = require("./desktopHarness");
const { spawnHost, createHostSession } = require("./fixtures/hostStack");
const { startRoutingIngress } = require("./fixtures/routingIngress");

const deps = desktopDepsAvailable();
const RECORD_DIR =
  process.env.OMNIGENT_DESKTOP_RECORD_DIR ||
  path.join(__dirname, "recordings", "desktop-workspace-download");
const FILE_NAME = "report.txt";
const FILE_BYTES = Buffer.from(
  Array.from({ length: 64 }, (_, i) => `line ${i}: workspace download probe\n`).join(""),
);
const DOWNLOAD_WAIT_MS = 10_000;

function sleep(ms) {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

/** A standalone SPA bundle with host routing on, built once unless supplied. */
function ensureRoutingDist(tmpDir) {
  const given = process.env.OMNIGENT_ROUTING_WEB_UI_DIST;
  if (given && fs.existsSync(path.join(given, "index.html"))) return given;
  const out = path.join(tmpDir, "web-ui-routing");
  const web = path.resolve(APP_ROOT, "..");
  const result = spawnSync(
    path.join(web, "node_modules", ".bin", "vite"),
    ["build", "--outDir", out, "--emptyOutDir"],
    { cwd: web, env: { ...process.env, VITE_OMNIGENT_HOST_ROUTING: "true" }, encoding: "utf8" },
  );
  if (result.status !== 0) {
    throw new Error(`routing SPA build failed:\n${(result.stderr || result.stdout).slice(-3000)}`);
  }
  return out;
}

/** Record every download the shell starts; `savePath` skips the native Save dialog. */
async function watchDownloads(electronApp, savePath) {
  await electronApp.evaluate(({ session }, target) => {
    globalThis.omniDownloadRecords = [];
    session.defaultSession.on("will-download", (_event, item) => {
      const record = {
        url: item.getURL(),
        filename: item.getFilename(),
        startState: item.getState(),
        done: null,
        receivedBytes: 0,
        savePath: null,
      };
      globalThis.omniDownloadRecords.push(record);
      if (target) item.setSavePath(target);
      item.on("done", (_e, state) => {
        record.done = state;
        record.receivedBytes = item.getReceivedBytes();
        record.savePath = item.getSavePath();
      });
    });
  }, savePath ?? null);
}

const readDownloads = (electronApp) =>
  electronApp.evaluate(() => globalThis.omniDownloadRecords ?? []);

async function waitForDownloadDone(electronApp, count) {
  const deadline = Date.now() + DOWNLOAD_WAIT_MS;
  let downloads = [];
  // oxlint-disable no-await-in-loop -- polling the main process.
  while (Date.now() < deadline) {
    downloads = await readDownloads(electronApp);
    if (downloads.length >= count && downloads.slice(count - 1).every((d) => d.done)) break;
    await sleep(250);
  }
  // oxlint-enable no-await-in-loop
  return downloads;
}

/** Navigate the shell to the session and open the Files tab; returns the row. */
async function openFilesPanel(window, baseUrl, sessionId) {
  await window.goto(`${baseUrl}/c/${sessionId}`);
  const toggle = window
    .locator('button[aria-label="Expand right panel"], button[aria-label="Collapse right panel"]')
    .first();
  await toggle.waitFor({ state: "visible", timeout: 60_000 });
  if ((await toggle.getAttribute("aria-label")) === "Expand right panel") await toggle.click();
  const rail = window.getByRole("complementary", { name: "Workspace" });
  await rail.waitFor({ state: "visible", timeout: 15_000 });
  await rail.getByRole("tab", { name: /^Files/ }).click();
  const row = rail.getByRole("button", { name: /report\.txt/ }).filter({ hasText: FILE_NAME });
  await row.waitFor({ state: "visible", timeout: 60_000 });
  return { rail, row };
}

/** What the user can see about a failed download right after the click. */
async function visibleErrorState(window, rail) {
  const icon = rail.getByRole("button", { name: `Download ${FILE_NAME}` });
  const iconClass = (await icon.count()) ? await icon.first().getAttribute("class") : null;
  const toast = window.getByText("Download failed");
  return {
    iconMarkedFailed: Boolean(iconClass && iconClass.includes("text-destructive")),
    failedToastVisible: (await toast.count()) > 0 && (await toast.first().isVisible()),
  };
}

describe(
  "desktop shell — workspace file download on a slice-key-routed deployment",
  { skip: deps.ok ? false : `missing deps: ${deps.missing.join(", ")}` },
  () => {
    let tmpDir;
    let server;
    let host;
    let ingress;
    let session;
    let workspace;

    before(async () => {
      tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "omni-desktop-download-"));
      fs.mkdirSync(RECORD_DIR, { recursive: true });
      const routingDist = ensureRoutingDist(tmpDir);
      server = await spawnServer(tmpDir, { env: () => ({ OMNIGENT_WEB_UI_DIST: routingDist }) });
      host = await spawnHost(server.serverUrl, tmpDir, {
        env: { OPENAI_BASE_URL: `${server.mockUrl}/v1`, OPENAI_API_KEY: "mock-key" },
      });
      workspace = path.join(tmpDir, "workspace");
      fs.mkdirSync(workspace, { recursive: true });
      fs.writeFileSync(path.join(workspace, FILE_NAME), FILE_BYTES);
      session = await createHostSession(server.serverUrl, { hostId: host.hostId, workspace });
      ingress = await startRoutingIngress(server.serverUrl, {
        logPath: path.join(RECORD_DIR, "ingress-requests.jsonl"),
      });
      fs.writeFileSync(
        path.join(RECORD_DIR, "stack.json"),
        JSON.stringify(
          {
            serverUrl: server.serverUrl,
            ingressUrl: ingress.url,
            hostId: host.hostId,
            sessionId: session.sessionId,
            runnerId: session.runnerId,
            workspace,
            routingDist,
          },
          null,
          2,
        ),
      );
    });

    after(async () => {
      if (ingress) await ingress.close();
      if (host) await host.close();
      if (server) await server.close();
      if (tmpDir) fs.rmSync(tmpDir, { recursive: true, force: true });
    });

    it("the shell's own requests through the ingress carry the host's slice key", async () => {
      const recordDir = path.join(RECORD_DIR, "listing");
      const { electronApp, window, userDataDir, stopDisplayCapture } = await launchDesktop({
        recordDir,
        serverUrl: ingress.url,
      });
      let summary;
      try {
        await window
          .getByText("What should we build?")
          .waitFor({ state: "visible", timeout: 30_000 });
        const { row } = await openFilesPanel(window, ingress.url, session.sessionId);
        assert.ok(await row.isVisible(), "workspace file row not visible");
        await window.screenshot({ path: path.join(recordDir, "files-panel.png") });
        const tunnel = ingress.requests.filter((r) => r.tunnelRoute);
        summary = {
          tunnelRequests: tunnel.length,
          keyedWithHost: tunnel.filter((r) => r.sliceKey === host.hostId).length,
          wrongReplica: tunnel.filter((r) => r.decision === "wrong-replica").map((r) => r.path),
          identityHeaders: [...new Set(ingress.requests.map((r) => r.forwardedEmail))],
        };
        fs.writeFileSync(
          path.join(recordDir, "ingress-summary.json"),
          JSON.stringify(summary, null, 2),
        );
      } finally {
        await electronApp.close();
        await stopDisplayCapture();
        saveRecording(recordDir, "listing");
        fs.rmSync(userDataDir, { recursive: true, force: true });
      }
      assert.ok(summary.keyedWithHost > 0, `no keyed tunnel requests: ${JSON.stringify(summary)}`);
    });

    it("downloading through the ingress carries the slice key and saves the complete file", async () => {
      const recordDir = path.join(RECORD_DIR, "routed");
      const saveTo = path.join(tmpDir, "saved-routed", FILE_NAME);
      // Wall-clock marks relative to the display capture start, for captioning.
      const captureStart = Date.now();
      const marks = {};
      const mark = (name) => {
        marks[name] = (Date.now() - captureStart) / 1000;
      };
      const { electronApp, window, userDataDir, stopDisplayCapture } = await launchDesktop({
        recordDir,
        serverUrl: ingress.url,
      });
      const observed = { rowIcon: {}, viewerMenu: {}, downloads: [], downloadRequests: [], marks };
      try {
        await watchDownloads(electronApp, saveTo);
        await window
          .getByText("What should we build?")
          .waitFor({ state: "visible", timeout: 30_000 });
        mark("shellConnected");
        await sleep(1500);
        const { rail, row } = await openFilesPanel(window, ingress.url, session.sessionId);
        mark("filesPanelListed");
        await sleep(2500);

        // 1. File-row download icon (hover-revealed).
        await row.hover();
        await sleep(1200);
        await rail.getByRole("button", { name: `Download ${FILE_NAME}` }).click();
        mark("rowIconClicked");
        let downloads = await waitForDownloadDone(electronApp, 1);
        observed.rowIcon = {
          downloads: downloads.length,
          ...(await visibleErrorState(window, rail)),
        };
        mark("rowIconObserved");
        await window.screenshot({ path: path.join(recordDir, "after-row-icon-click.png") });
        await sleep(2000);

        // 2. File viewer > View settings > Download file.
        await row.click();
        await rail.getByTestId("file-viewer").waitFor({ state: "visible", timeout: 15_000 });
        mark("viewerOpened");
        await sleep(1500);
        await rail.getByRole("button", { name: "View settings" }).click();
        mark("viewSettingsOpened");
        await sleep(1200);
        await window.getByRole("menuitem", { name: "Download file" }).click();
        mark("viewerDownloadClicked");
        downloads = await waitForDownloadDone(electronApp, 2);
        observed.viewerMenu = {
          downloads: downloads.length,
          failedToastVisible: (await window.getByText("Download failed").count()) > 0,
        };
        mark("viewerDownloadObserved");
        await window.screenshot({ path: path.join(recordDir, "after-viewer-download.png") });
        await sleep(3000);
        mark("end");
        observed.downloads = downloads;
        observed.downloadRequests = ingress.requests.filter((r) =>
          r.path.includes("download=true"),
        );
        observed.savedBytes = fs.existsSync(saveTo) ? fs.readFileSync(saveTo).length : null;
        fs.writeFileSync(path.join(recordDir, "observed.json"), JSON.stringify(observed, null, 2));
      } finally {
        await electronApp.close();
        await stopDisplayCapture();
        saveRecording(recordDir, "routed-download");
        fs.rmSync(userDataDir, { recursive: true, force: true });
      }
      const requests = observed.downloadRequests;
      assert.ok(
        requests.length >= 2,
        `expected two download requests at the ingress, got ${requests.length}`,
      );
      for (const request of requests) {
        assert.equal(
          request.sliceKey,
          host.hostId,
          `download request bypassed the app transport (no slice key): ${JSON.stringify(request)}`,
        );
      }
      assert.ok(
        observed.downloads.some((d) => d.done === "completed"),
        `no completed download; observed ${JSON.stringify(observed)}`,
      );
      assert.equal(observed.savedBytes, FILE_BYTES.length, "saved file is not the complete file");
    });

    it("downloading directly from the server saves the complete file", async () => {
      const recordDir = path.join(RECORD_DIR, "direct");
      const saveTo = path.join(tmpDir, "saved-direct", FILE_NAME);
      const { electronApp, window, userDataDir, stopDisplayCapture } = await launchDesktop({
        recordDir,
        serverUrl: server.serverUrl,
      });
      let downloads;
      try {
        await watchDownloads(electronApp, saveTo);
        await window
          .getByText("What should we build?")
          .waitFor({ state: "visible", timeout: 30_000 });
        const { rail, row } = await openFilesPanel(window, server.serverUrl, session.sessionId);
        await sleep(1500);
        await row.hover();
        await sleep(800);
        await rail.getByRole("button", { name: `Download ${FILE_NAME}` }).click();
        downloads = await waitForDownloadDone(electronApp, 1);
        await sleep(2000);
        fs.writeFileSync(
          path.join(recordDir, "observed.json"),
          JSON.stringify({ downloads }, null, 2),
        );
      } finally {
        await electronApp.close();
        await stopDisplayCapture();
        saveRecording(recordDir, "direct-download");
        fs.rmSync(userDataDir, { recursive: true, force: true });
      }
      assert.equal(downloads.length, 1, `expected one download, got ${JSON.stringify(downloads)}`);
      assert.equal(downloads[0].done, "completed", JSON.stringify(downloads[0]));
      assert.ok(
        fs.existsSync(saveTo) && fs.readFileSync(saveTo).equals(FILE_BYTES),
        "saved bytes differ",
      );
    });
  },
);
