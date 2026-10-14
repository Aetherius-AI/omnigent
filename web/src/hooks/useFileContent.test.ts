// Tests for downloadWorkspaceFile's two transports under a configured base path.
//
//   - Standalone browser: a direct anchor click, so the URL must carry the
//     subpath prefix (withBasePath) or a stripping proxy misses the app.
//   - Managed / mobile: authenticatedFetch owns the transport and prefixes
//     internally, so it must receive the RAW url; prefixing here would double it.
//
// The seams (`@/lib/host`, `@/lib/identity`, `@/lib/nativeBridge`, the workspace
// path helpers, the chat store) are mocked; `withBasePath` is the real thing,
// reading `window.__OMNIGENT_BASE_PATH__`.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const isDatabricksWorkspace = vi.fn();
const isHostRoutingEnabled = vi.fn(() => false);
const authenticatedFetch = vi.fn();
const isIOSShell = vi.fn(() => false);
const isAndroidShell = vi.fn(() => false);
const isElectronShell = vi.fn(() => false);

vi.mock("@/lib/host", () => ({
  isDatabricksWorkspace: () => isDatabricksWorkspace(),
  isHostRoutingEnabled: () => isHostRoutingEnabled(),
}));
vi.mock("@/lib/identity", () => ({ authenticatedFetch: (u: string) => authenticatedFetch(u) }));
vi.mock("@/lib/nativeBridge", () => ({
  isIOSShell: () => isIOSShell(),
  isAndroidShell: () => isAndroidShell(),
  isElectronShell: () => isElectronShell(),
}));
vi.mock("@/hooks/useWorkspaceChangedFiles", () => ({
  browseLocationBase: () => "",
  browseLocationSegment: (p: string) => p,
  useWorkspaceServeable: () => ({}),
}));
vi.mock("@/store/chatStore", () => ({ useChatStore: () => undefined }));

import { downloadWorkspaceFile } from "./useFileContent";

const RAW_URL =
  "/v1/sessions/sess_abc/resources/environments/default/filesystem/src/main.py?download=true";

beforeEach(() => {
  isDatabricksWorkspace.mockReset();
  isHostRoutingEnabled.mockReturnValue(false);
  authenticatedFetch.mockReset();
  isIOSShell.mockReturnValue(false);
  isAndroidShell.mockReturnValue(false);
  isElectronShell.mockReturnValue(false);
  // Stub the click so jsdom doesn't attempt a navigation for the download anchor.
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  delete window.__OMNIGENT_BASE_PATH__;
});

describe("downloadWorkspaceFile base-path handling", () => {
  it("prefixes the standalone download anchor with the configured base path", async () => {
    window.__OMNIGENT_BASE_PATH__ = "/proxy/6767";
    isDatabricksWorkspace.mockReturnValue(false);
    const appendSpy = vi.spyOn(document.body, "append").mockImplementation(() => {});

    await downloadWorkspaceFile("sess_abc", "src/main.py");

    expect(appendSpy).toHaveBeenCalledTimes(1);
    const anchor = appendSpy.mock.calls[0]?.[0] as HTMLAnchorElement;
    expect(anchor.getAttribute("href")).toBe(`/proxy/6767${RAW_URL}`);
    expect(authenticatedFetch).not.toHaveBeenCalled();
  });

  it("passes the raw url to authenticatedFetch on the managed branch (no double prefix)", async () => {
    window.__OMNIGENT_BASE_PATH__ = "/proxy/6767";
    isDatabricksWorkspace.mockReturnValue(true);
    authenticatedFetch.mockResolvedValue({ ok: true, blob: async () => new Blob(["x"]) });
    vi.stubGlobal("URL", { createObjectURL: () => "blob:x", revokeObjectURL: () => {} });

    await downloadWorkspaceFile("sess_abc", "src/main.py");

    expect(authenticatedFetch).toHaveBeenCalledWith(RAW_URL);
  });
});

// The desktop shell is a standalone client whose anchors cannot carry the slice
// key authenticatedFetch stamps on a host-routed deployment, so the download
// must take the authenticated transport and surface its HTTP failures.
describe("downloadWorkspaceFile in the Electron shell on a host-routed deployment", () => {
  beforeEach(() => {
    isDatabricksWorkspace.mockReturnValue(false);
    isHostRoutingEnabled.mockReturnValue(true);
    isElectronShell.mockReturnValue(true);
    vi.stubGlobal("URL", { createObjectURL: () => "blob:x", revokeObjectURL: () => {} });
  });

  it("fetches the file through authenticatedFetch instead of a raw server anchor", async () => {
    authenticatedFetch.mockResolvedValue({ ok: true, blob: async () => new Blob(["x"]) });
    const appendSpy = vi.spyOn(document.body, "append").mockImplementation(() => {});

    await downloadWorkspaceFile("sess_abc", "src/main.py");

    expect(authenticatedFetch).toHaveBeenCalledWith(RAW_URL);
    const anchor = appendSpy.mock.calls[0]?.[0] as HTMLAnchorElement | undefined;
    expect(anchor?.getAttribute("href")).toBe("blob:x");
  });

  it("rejects on an HTTP failure so the caller can show the download error", async () => {
    authenticatedFetch.mockResolvedValue({ ok: false, status: 502, statusText: "Bad Gateway" });
    vi.spyOn(document.body, "append").mockImplementation(() => {});

    await expect(downloadWorkspaceFile("sess_abc", "src/main.py")).rejects.toThrow(
      "502 Bad Gateway",
    );
  });
});
