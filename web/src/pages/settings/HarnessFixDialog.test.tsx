import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
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

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

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
  vi.spyOn(window, "open").mockImplementation(() => null);
});

describe("HarnessFixDialog", () => {
  it("chooses Isaac, shows progress, then reports success", async () => {
    const response = deferred<Response>();
    vi.mocked(authenticatedFetch).mockReturnValue(response.promise);
    const props = renderDialog();
    const user = userEvent.setup();

    expect(screen.getByRole("heading", { name: "Reconfigure Codex" })).toBeVisible();
    expect(screen.getByRole("radio", { name: /Isaac/ })).toHaveAttribute("aria-checked", "true");
    await user.click(screen.getByRole("button", { name: "Authenticate" }));

    expect(screen.getByRole("heading", { name: "Reconfiguring Codex…" })).toBeVisible();
    expect(screen.getByText("Connected to the Databricks Unity Gateway")).toBeVisible();
    expect(screen.getByText("Saving the credential on Local laptop")).toBeVisible();
    expect(authenticatedFetch).toHaveBeenCalledWith(
      "/v1/hosts/host%2F1/harnesses/codex-native/credential",
      expect.objectContaining({ body: JSON.stringify({ kind: "gateway" }) }),
    );

    response.resolve(
      json({
        object: "harness_credential",
        harness: "codex-native",
        configured_harnesses: { "codex-native": true },
      }),
    );
    await waitFor(() =>
      expect(props.onFixed).toHaveBeenCalledWith({
        configuredHarnesses: { "codex-native": true },
        credentialLabel: "Isaac",
      }),
    );
    expect(showToast).toHaveBeenCalledWith("Codex is ready on Local laptop");
    expect(props.onOpenChange).toHaveBeenCalledWith(false);
  });

  it("opens personal sign-in and polls until it succeeds", async () => {
    const status = deferred<Response>();
    vi.mocked(authenticatedFetch).mockImplementation(async (input) => {
      const url = String(input);
      if (url.endsWith("/login")) {
        return json({
          login_id: "login 1",
          auth_url: "https://auth.example/device",
          user_code: "ABCD-EFGH",
          status: "pending",
        });
      }
      if (url.endsWith("/login/login%201")) return status.promise;
      throw new Error(`Unexpected URL: ${url}`);
    });
    const props = renderDialog();
    const user = userEvent.setup();

    await user.click(screen.getByRole("radio", { name: /Personal subscription/ }));
    await user.click(screen.getByRole("button", { name: "Authenticate" }));

    expect(await screen.findByRole("link", { name: "Open the sign-in page" })).toHaveAttribute(
      "href",
      "https://auth.example/device",
    );
    expect(screen.getByText("ABCD-EFGH")).toBeVisible();
    expect(screen.getByText("ABCD-EFGH")).toHaveClass("text-base", "font-semibold");
    await user.click(screen.getByRole("button", { name: "Copy sign-in code" }));
    expect(copyText).toHaveBeenCalledWith("ABCD-EFGH");
    expect(window.open).toHaveBeenCalledWith(
      "https://auth.example/device",
      "_blank",
      "noopener,noreferrer",
    );

    status.resolve(
      json({
        status: "succeeded",
        error: null,
        configured_harnesses: { "codex-native": true },
      }),
    );
    await waitFor(() =>
      expect(props.onFixed).toHaveBeenCalledWith({
        configuredHarnesses: { "codex-native": true },
        credentialLabel: "Personal subscription",
      }),
    );
  });

  it("shows the server reason and Try again preserves the credential choice", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValue(
      json(
        { detail: "Local laptop couldn’t reach the Databricks Unity Gateway." },
        { status: 502 },
      ),
    );
    renderDialog();
    const user = userEvent.setup();

    await user.click(screen.getByRole("radio", { name: /Personal subscription/ }));
    await user.click(screen.getByRole("button", { name: "Authenticate" }));

    expect(
      await screen.findByText("Local laptop couldn’t reach the Databricks Unity Gateway."),
    ).toBeVisible();
    expect(screen.getByText("omni setup")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(screen.getByRole("radio", { name: /Personal subscription/ })).toHaveAttribute(
      "aria-checked",
      "true",
    );
  });

  it("starts the update path immediately and requests an upgrade", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValue(
      json({
        object: "harness_install",
        harness: "opencode-native",
        configured_harnesses: { "opencode-native": true },
        version: "2.0.0",
      }),
    );
    const props = renderDialog({
      mode: "update",
      harness: "opencode-native",
      harnessLabel: "OpenCode",
      currentVersion: "1.0.0",
    });

    expect(screen.getByRole("heading", { name: "Updating OpenCode…" })).toBeVisible();
    expect(screen.getByText(/Updating OpenCode from 1.0.0/)).toBeVisible();
    await waitFor(() =>
      expect(authenticatedFetch).toHaveBeenCalledWith(
        "/v1/hosts/host%2F1/harnesses/opencode-native/install",
        expect.objectContaining({ body: JSON.stringify({ upgrade: true }) }),
      ),
    );
    await waitFor(() =>
      expect(props.onFixed).toHaveBeenCalledWith({
        configuredHarnesses: { "opencode-native": true },
        credentialLabel: undefined,
      }),
    );
  });

  it("installs first and then applies the selected credential", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(async (input) => {
      const url = String(input);
      if (url.endsWith("/install")) {
        return json({
          object: "harness_install",
          harness: "codex-native",
          configured_harnesses: { "codex-native": "needs-auth" },
          version: "1.0.0",
        });
      }
      if (url.endsWith("/credential")) {
        return json({
          object: "harness_credential",
          harness: "codex-native",
          configured_harnesses: { "codex-native": true },
        });
      }
      throw new Error(`Unexpected URL: ${url}`);
    });
    const props = renderDialog({ mode: "install" });
    const user = userEvent.setup();

    expect(screen.getByRole("heading", { name: "Install Codex" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Install" }));

    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(2));
    expect(props.onFixed).toHaveBeenCalledWith({
      configuredHarnesses: { "codex-native": true },
      credentialLabel: "Isaac",
    });
  });

  it("uses the omni setup fallback for Pi while its login flow is unsupported", () => {
    renderDialog({ harness: "pi-native", harnessLabel: "Pi" });

    expect(screen.getByText(/Run/)).toHaveTextContent(
      "Run omni setup on Local laptop to finish setting up Pi.",
    );
    expect(screen.queryByRole("radio")).not.toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Close" })[0]).toBeVisible();
  });

  it("cancels a pending browser login when closed", async () => {
    const status = deferred<Response>();
    vi.mocked(authenticatedFetch).mockImplementation(async (input, init) => {
      const url = String(input);
      if (url.endsWith("/login")) {
        return json({ login_id: "pending", auth_url: null, user_code: null, status: "pending" });
      }
      if (init?.method === "DELETE") return json({});
      return status.promise;
    });
    const props = renderDialog();
    const user = userEvent.setup();

    await user.click(screen.getByRole("radio", { name: /Personal subscription/ }));
    await user.click(screen.getByRole("button", { name: "Authenticate" }));
    await screen.findByText("Waiting for you to finish in the browser");
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    await waitFor(() =>
      expect(authenticatedFetch).toHaveBeenCalledWith(
        "/v1/hosts/host%2F1/harnesses/codex-native/login/pending",
        { method: "DELETE" },
      ),
    );
    expect(props.onOpenChange).toHaveBeenCalledWith(false);
    await act(async () => status.resolve(json({ status: "pending" })));
  });
});
