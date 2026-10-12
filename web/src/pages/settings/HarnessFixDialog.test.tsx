import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { showToast } from "@/components/ui/toast";
import { copyText } from "@/lib/clipboard";
import { authenticatedFetch } from "@/lib/identity";
import { HarnessFixDialog, type HarnessFixDialogProps } from "./HarnessFixDialog";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));
vi.mock("@/components/ui/toast", () => ({ showToast: vi.fn() }));
vi.mock("@/lib/clipboard", () => ({ copyText: vi.fn().mockResolvedValue(undefined) }));

function json(body: unknown, init: ResponseInit = {}) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
    ...init,
  });
}

const catalog = {
  data: [],
  setup_steps: {
    "codex-native": [
      {
        kind: "auth",
        title: "Sign in",
        detail: "",
        action: "command",
        command: "codex login",
        status_key: "authed",
      },
    ],
    "claude-native": [
      {
        kind: "auth",
        title: "Sign in",
        detail: "",
        action: "command",
        command: "claude auth login --claudeai",
        status_key: "authed",
      },
    ],
    "pi-native": [
      {
        kind: "auth",
        title: "Sign in",
        detail: "",
        action: "command",
        command: null,
        status_key: "authed",
      },
    ],
  },
};

const baseProps: HarnessFixDialogProps = {
  open: true,
  onOpenChange: vi.fn(),
  mode: "reconfigure",
  hostId: "host/1",
  hostName: "Local laptop",
  harness: "codex-native",
  harnessLabel: "Codex",
  onFixed: vi.fn(),
};

function renderDialog(overrides: Partial<HarnessFixDialogProps> = {}) {
  const props = { ...baseProps, ...overrides };
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <HarnessFixDialog {...props} />
    </QueryClientProvider>,
  );
  return props;
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(authenticatedFetch).mockImplementation(async (input) => {
    if (String(input) === "/v1/harnesses") return json(catalog);
    throw new Error(`Unexpected URL: ${String(input)}`);
  });
});

describe("HarnessFixDialog", () => {
  it("shows the Isaac setup fallback without calling the credential endpoint", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      throw new Error(`Unexpected URL: ${url} ${init?.method}`);
    });
    renderDialog();
    const user = userEvent.setup();

    expect(screen.getByRole("heading", { name: "Reconfigure Codex" })).toBeVisible();
    expect(screen.getByRole("radio", { name: /Isaac/ })).toHaveAttribute("aria-checked", "true");
    await user.click(screen.getByRole("button", { name: "Authenticate" }));

    expect(
      screen.getByRole("heading", { name: "Set up Isaac for Codex on Local laptop" }),
    ).toBeVisible();
    expect(
      screen.getByText("Run this in a terminal on Local laptop, then come back."),
    ).toBeVisible();
    expect(screen.getByText("omni setup")).toBeVisible();
    expect(
      screen.getByText("Choose Codex, then the Databricks Unity Gateway credential."),
    ).toBeVisible();
    expect(screen.getAllByRole("button", { name: "Close" })).toHaveLength(2);
    expect(screen.getByRole("button", { name: "Check again" })).toBeVisible();
    expect(
      vi
        .mocked(authenticatedFetch)
        .mock.calls.every(([input]) => !String(input).includes("/credential")),
    ).toBe(true);
  });

  it("finishes Isaac setup when Check again reports gateway readiness", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      if (url.endsWith("/harnesses/refresh")) {
        expect(init).toEqual({ method: "POST" });
        return json({
          configured_harnesses: { "codex-native": true },
          gateway_inference: { "codex-native": true },
        });
      }
      throw new Error(`Unexpected URL: ${url}`);
    });
    const props = renderDialog();
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: "Authenticate" }));
    await user.click(screen.getByRole("button", { name: "Check again" }));

    await waitFor(() =>
      expect(props.onFixed).toHaveBeenCalledWith({
        configuredHarnesses: { "codex-native": true },
        credentialLabel: "Isaac",
      }),
    );
    expect(showToast).toHaveBeenCalledWith("Codex is ready on Local laptop");
    expect(props.onOpenChange).toHaveBeenCalledWith(false);
  });

  it("keeps Isaac setup open when Check again is not ready", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      if (url.endsWith("/harnesses/refresh")) {
        return json({
          configured_harnesses: { "codex-native": "needs-auth" },
          gateway_inference: { "codex-native": false },
        });
      }
      throw new Error(`Unexpected URL: ${url}`);
    });
    const props = renderDialog();
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: "Authenticate" }));
    await user.click(screen.getByRole("button", { name: "Check again" }));

    expect(await screen.findByText("Still not signed in on Local laptop.")).toBeVisible();
    expect(props.onFixed).not.toHaveBeenCalled();
    expect(props.onOpenChange).not.toHaveBeenCalledWith(false);
  });

  it("shows Claude's server command and succeeds after Check again", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      if (url.endsWith("/harnesses/refresh")) {
        expect(init).toEqual({ method: "POST" });
        return json({
          configured_harnesses: { "claude-native": true },
          gateway_inference: null,
        });
      }
      throw new Error(`Unexpected URL: ${url}`);
    });
    const props = renderDialog({ harness: "claude-native", harnessLabel: "Claude Code" });
    const user = userEvent.setup();

    await screen.findByRole("radio", { name: /Personal subscription/ });
    await user.click(screen.getByRole("radio", { name: /Personal subscription/ }));
    await user.click(screen.getByRole("button", { name: "Authenticate" }));

    expect(
      screen.getByRole("heading", { name: "Sign in to Claude Code on Local laptop" }),
    ).toBeVisible();
    expect(screen.getByText("claude auth login --claudeai")).toBeVisible();
    expect(
      screen.getByText(
        "Open the link it prints, then paste the code from your browser back into the terminal.",
      ),
    ).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Copy login command" }));
    expect(copyText).toHaveBeenCalledWith("claude auth login --claudeai");
    await user.click(screen.getByRole("button", { name: "Check again" }));

    await waitFor(() =>
      expect(props.onFixed).toHaveBeenCalledWith({
        configuredHarnesses: { "claude-native": true },
        credentialLabel: "Personal subscription",
      }),
    );
  });

  it("keeps Codex open with an inline message when Check again is not ready", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      if (url.endsWith("/harnesses/refresh")) {
        return json({
          configured_harnesses: { "codex-native": "needs-auth" },
          gateway_inference: { "codex-native": false },
        });
      }
      throw new Error(`Unexpected URL: ${url}`);
    });
    const props = renderDialog();
    const user = userEvent.setup();

    await screen.findByRole("radio", { name: /Personal subscription/ });
    await user.click(screen.getByRole("radio", { name: /Personal subscription/ }));
    await user.click(screen.getByRole("button", { name: "Authenticate" }));

    expect(screen.getByText("codex login --device-auth")).toBeVisible();
    expect(screen.getByText("Open the link it prints and enter the one-time code.")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Check again" }));
    expect(await screen.findByText("Still not signed in on Local laptop.")).toBeVisible();
    expect(props.onFixed).not.toHaveBeenCalled();
    expect(props.onOpenChange).not.toHaveBeenCalledWith(false);
  });

  it("shows a refresh failure in the error state", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      if (url.endsWith("/harnesses/refresh")) {
        return json({ detail: "The host readiness check timed out." }, { status: 504 });
      }
      throw new Error(`Unexpected URL: ${url}`);
    });
    const user = userEvent.setup();
    renderDialog();

    await screen.findByRole("radio", { name: /Personal subscription/ });
    await user.click(screen.getByRole("radio", { name: /Personal subscription/ }));
    await user.click(screen.getByRole("button", { name: "Authenticate" }));
    await user.click(screen.getByRole("button", { name: "Check again" }));

    expect(await screen.findByText("The host readiness check timed out.")).toBeVisible();
    expect(screen.getByRole("heading", { name: "Couldn’t reconfigure Codex" })).toBeVisible();
  });

  it("preserves the selected credential after an error and Try again", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      if (url.endsWith("/harnesses/refresh")) {
        return json({ detail: "Refresh failed." }, { status: 500 });
      }
      throw new Error(`Unexpected URL: ${url}`);
    });
    renderDialog();
    const user = userEvent.setup();

    await screen.findByRole("radio", { name: /Personal subscription/ });
    await user.click(screen.getByRole("radio", { name: /Personal subscription/ }));
    await user.click(screen.getByRole("button", { name: "Authenticate" }));
    await user.click(screen.getByRole("button", { name: "Check again" }));
    await user.click(await screen.findByRole("button", { name: "Try again" }));

    expect(screen.getByRole("radio", { name: /Personal subscription/ })).toHaveAttribute(
      "aria-checked",
      "true",
    );
  });

  it("updates with a bodyless install request and accepts a null version", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      if (url.endsWith("/install")) {
        expect(init).toEqual({ method: "POST" });
        return json({
          object: "harness_install",
          harness: "opencode-native",
          configured_harnesses: { "opencode-native": true },
          version: null,
        });
      }
      throw new Error(`Unexpected URL: ${url}`);
    });
    const props = renderDialog({
      mode: "update",
      harness: "opencode-native",
      harnessLabel: "OpenCode",
      currentVersion: "1.0.0",
    });

    expect(screen.getByRole("heading", { name: "Updating OpenCode…" })).toBeVisible();
    expect(screen.getByText(/Updating OpenCode from 1.0.0/)).toBeVisible();
    await waitFor(() =>
      expect(props.onFixed).toHaveBeenCalledWith({
        configuredHarnesses: { "opencode-native": true },
        credentialLabel: undefined,
      }),
    );
  });

  it("installs without a body and then shows the selected Isaac setup", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      if (url.endsWith("/install")) {
        expect(init).toEqual({ method: "POST" });
        return json({
          object: "harness_install",
          harness: "codex-native",
          configured_harnesses: { "codex-native": "needs-auth" },
          version: "1.0.0",
        });
      }
      if (url.endsWith("/harnesses/refresh")) {
        expect(init).toEqual({ method: "POST" });
        return json({
          configured_harnesses: { "codex-native": true },
          gateway_inference: { "codex-native": true },
        });
      }
      throw new Error(`Unexpected URL: ${url}`);
    });
    const props = renderDialog({ mode: "install" });
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: "Install" }));
    expect(
      await screen.findByRole("heading", { name: "Set up Isaac for Codex on Local laptop" }),
    ).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Check again" }));
    await waitFor(() => expect(props.onFixed).toHaveBeenCalled());
    expect(props.onFixed).toHaveBeenCalledWith({
      configuredHarnesses: { "codex-native": true },
      credentialLabel: "Isaac",
    });
  });

  it("uses omni setup when Pi's authenticated catalog command is null", async () => {
    renderDialog({ harness: "pi-native", harnessLabel: "Pi" });
    const user = userEvent.setup();

    expect(screen.getByRole("radio", { name: /Isaac/ })).toBeVisible();
    await user.click(screen.getByRole("radio", { name: /Personal subscription/ }));
    await user.click(screen.getByRole("button", { name: "Authenticate" }));

    expect(screen.getByText("omni setup")).toBeVisible();
  });

  it("keeps API key and gateway forms reachable for other harnesses", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/v1/harnesses") return json(catalog);
      if (url.endsWith("/credentials/detected")) return json({ credentials: [] });
      throw new Error(`Unexpected URL: ${url}`);
    });
    renderDialog({ harness: "opencode-native", harnessLabel: "OpenCode" });

    expect(screen.getByText("Use an API key")).toBeVisible();
    expect(screen.getByText("Connect a gateway")).toBeVisible();
    expect(screen.getByPlaceholderText("Paste an API key")).toBeVisible();
    expect(screen.getByPlaceholderText(/Gateway base URL/)).toBeVisible();
    expect(screen.queryByText("Sign in with your subscription")).not.toBeInTheDocument();
  });
});
