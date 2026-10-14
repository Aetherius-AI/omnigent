// Desktop e2e: downloading a workspace file from the Files panel when the
// deployment routes host-scoped requests by the app's slice-key header (local
// server + real host behind the routing-ingress stand-in from fixtures/).

// Run from web/electron after building the SPA:
//   OMNIGENT_PYTHON=../../.venv/bin/python OMNIGENT_PW_NO_SANDBOX=1 \
//     xvfb-run -a node --test e2e/desktop_workspace_download.e2e.js
// Optional: OMNIGENT_ROUTING_WEB_UI_DIST (prebuilt routing bundle) and
// OMNIGENT_DESKTOP_RECORD_DIR (recordings + ingress log; paces the journey).

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

// Pauses that keep a recorded journey legible; skipped unless a record dir is chosen.
const PACED = Boolean(process.env.OMNIGENT_DESKTOP_RECORD_DIR);
const pace = (ms) => (PACED ? sleep(ms) : Promise.resolve());

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

/** Record every download the shell starts, saving each to its own file under `saveDir`. */
async function watchDownloads(electronApp, saveDir) {
  fs.mkdirSync(saveDir, { recursive: true });
  await electronApp.evaluate(({ session }, dir) => {
    globalThis.omniDownloadRecords = [];
    session.defaultSession.on("will-download", (_event, item) => {
      const record = {
        url: item.getURL(),
        filename: item.getFilename(),
        startState: item.getState(),
        done: null,
        receivedBytes: 0,
        savePath: `${dir}/${globalThis.omniDownloadRecords.length}-${item.getFilename()}`,
      };
      globalThis.omniDownloadRecords.push(record);
      // A preset path skips the native Save dialog.
      item.setSavePath(record.savePath);
      item.on("done", (_e, state) => {
        record.done = state;
        record.receivedBytes = item.getReceivedBytes();
      });
    });
  }, saveDir);
}

const readDownloads = (electronApp) =>
  electronApp.evaluate(() => globalThis.omniDownloadRecords ?? []);

/** Resolve once the first `count` downloads have finished; fail if they do not in time. */
async function waitForDownloadDone(electronApp, count) {
  const deadline = Date.now() + DOWNLOAD_WAIT_MS;
  // oxlint-disable no-await-in-loop -- polling the main process.
  for (;;) {
    const downloads = await readDownloads(electronApp);
    if (downloads.length >= count && downloads.slice(0, count).every((d) => d.done)) {
      return downloads;
    }
    if (Date.now() >= deadline) {
      throw new Error(
        `${count} download(s) did not finish within ${DOWNLOAD_WAIT_MS} ms: ${JSON.stringify(downloads)}`,
      );
    }
    await sleep(250);
  }
  // oxlint-enable no-await-in-loop
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
  const row = rail.getByRole("button", { name: FILE_NAME }).filter({ hasText: FILE_NAME });
  await row.waitFor({ state: "visible", timeout: 60_000 });
  return { rail, row };
}

/** Tunnel-dependent requests the ingress has seen so far, by how they were keyed. */
function tunnelRequestSummary(ingress, hostId) {
  const tunnel = ingress.requests.filter((r) => r.tunnelRoute);
  return {
    tunnelRequests: tunnel.length,
    keyedWithHost: tunnel.filter((r) => r.sliceKey === hostId).length,
    wrongReplica: tunnel.filter((r) => r.decision === "wrong-replica").map((r) => r.path),
  };
}

/** What the user can see about a failed download right after the click. */
async function visibleErrorState(window, rail) {
  // The row icon's only failure signal is its destructive styling (plus a tooltip).
  const icon = rail.getByRole("button", { name: `Download ${FILE_NAME}` });
  const iconClass = (await icon.count()) ? await icon.first().getAttribute("class") : null;
  const toast = window.getByText("Download failed");
  return {
    iconMarkedFailed: Boolean(iconClass && iconClass.includes("text-destructive")),
    failedToastVisible: (await toast.count()) > 0 && (await toast.first().isVisible()),
  };
}

function assertSavedFile(download) {
  assert.equal(
    download.done,
    "completed",
    `download did not complete: ${JSON.stringify(download)}`,
  );
  assert.ok(
    fs.existsSync(download.savePath) && fs.readFileSync(download.savePath).equals(FILE_BYTES),
    `saved file differs from the workspace file: ${download.savePath}`,
  );
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
      // Close everything even when one step throws, so no process or tmp dir leaks.
      const results = await Promise.allSettled([ingress?.close(), host?.close(), server?.close()]);
      if (tmpDir) fs.rmSync(tmpDir, { recursive: true, force: true });
      const failed = results.find((r) => r.status === "rejected");
      if (failed) throw failed.reason;
    });

    it("downloading through the ingress carries the slice key and saves the complete file", async () => {
      const recordDir = path.join(RECORD_DIR, "routed");
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
      const observed = {
        listing: null,
        rowIcon: {},
        viewerMenu: {},
        downloads: [],
        downloadRequests: [],
        marks,
      };
      try {
        await watchDownloads(electronApp, path.join(tmpDir, "saved-routed"));
        await window
          .getByText("What should we build?")
          .waitFor({ state: "visible", timeout: 30_000 });
        mark("shellConnected");
        await pace(1500);
        const { rail, row } = await openFilesPanel(window, ingress.url, session.sessionId);
        mark("filesPanelListed");
        observed.listing = tunnelRequestSummary(ingress, host.hostId);
        await pace(2500);

        // 1. File-row download icon (hover-revealed).
        await row.hover();
        await pace(1200);
        await rail.getByRole("button", { name: `Download ${FILE_NAME}` }).click();
        mark("rowIconClicked");
        await waitForDownloadDone(electronApp, 1);
        observed.rowIcon = await visibleErrorState(window, rail);
        mark("rowIconObserved");
        await window.screenshot({ path: path.join(recordDir, "after-row-icon-click.png") });
        await pace(2000);

        // 2. File viewer > View settings > Download file.
        await row.click();
        await rail.getByTestId("file-viewer").waitFor({ state: "visible", timeout: 15_000 });
        mark("viewerOpened");
        await pace(1500);
        await rail.getByRole("button", { name: "View settings" }).click();
        mark("viewSettingsOpened");
        await pace(1200);
        await window.getByRole("menuitem", { name: "Download file" }).click();
        mark("viewerDownloadClicked");
        await waitForDownloadDone(electronApp, 2);
        observed.viewerMenu = await visibleErrorState(window, rail);
        mark("viewerDownloadObserved");
        await window.screenshot({ path: path.join(recordDir, "after-viewer-download.png") });
        await pace(3000);
        mark("end");
      } finally {
        observed.downloads = await readDownloads(electronApp).catch(() => observed.downloads);
        observed.downloadRequests = ingress.requests.filter((r) =>
          r.path.includes("download=true"),
        );
        fs.writeFileSync(path.join(recordDir, "observed.json"), JSON.stringify(observed, null, 2));
        // Stop filming before the window closes so the clip ends on the app state.
        await stopDisplayCapture();
        await electronApp.close();
        saveRecording(recordDir, "routed-download");
        fs.rmSync(userDataDir, { recursive: true, force: true });
      }
      // The app's own listing requests routed by key, so a keyless download is a bypass.
      assert.ok(
        observed.listing.keyedWithHost > 0,
        `no keyed listing requests: ${JSON.stringify(observed.listing)}`,
      );
      assert.deepEqual(
        observed.listing.wrongReplica,
        [],
        "listing requests reached a wrong replica",
      );
      assert.equal(
        observed.downloadRequests.length,
        2,
        `expected one download request per action: ${JSON.stringify(observed.downloadRequests)}`,
      );
      for (const request of observed.downloadRequests) {
        assert.equal(
          request.sliceKey,
          host.hostId,
          `download request bypassed the app transport (no slice key): ${JSON.stringify(request)}`,
        );
      }
      assert.equal(
        observed.downloads.length,
        2,
        `expected one download per action: ${JSON.stringify(observed.downloads)}`,
      );
      for (const download of observed.downloads) assertSavedFile(download);
      assert.deepEqual(
        observed.rowIcon,
        { iconMarkedFailed: false, failedToastVisible: false },
        "the row icon reported a failure",
      );
      assert.equal(
        observed.viewerMenu.failedToastVisible,
        false,
        "the viewer showed Download failed",
      );
    });

    it("downloading directly from the server saves the complete file", async () => {
      const recordDir = path.join(RECORD_DIR, "direct");
      const { electronApp, window, userDataDir, stopDisplayCapture } = await launchDesktop({
        recordDir,
        serverUrl: server.serverUrl,
      });
      let downloads = [];
      try {
        await watchDownloads(electronApp, path.join(tmpDir, "saved-direct"));
        await window
          .getByText("What should we build?")
          .waitFor({ state: "visible", timeout: 30_000 });
        const { rail, row } = await openFilesPanel(window, server.serverUrl, session.sessionId);
        await pace(1500);
        await row.hover();
        await pace(800);
        await rail.getByRole("button", { name: `Download ${FILE_NAME}` }).click();
        downloads = await waitForDownloadDone(electronApp, 1);
        await pace(2000);
        fs.writeFileSync(
          path.join(recordDir, "observed.json"),
          JSON.stringify({ downloads }, null, 2),
        );
      } finally {
        await stopDisplayCapture();
        await electronApp.close();
        saveRecording(recordDir, "direct-download");
        fs.rmSync(userDataDir, { recursive: true, force: true });
      }
      assert.equal(downloads.length, 1, `expected one download, got ${JSON.stringify(downloads)}`);
      assertSavedFile(downloads[0]);
    });
  },
);
