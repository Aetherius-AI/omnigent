import { renderSidebar } from "@/test/sidebarTestHelpers";
import { conversation as conv, conversationPage } from "@/test/sidebarMockHelpers";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/hooks/useScopeCache", () => import("@/test/mockScopeCache"));

// The session list tells the user that sessions are being imported while
// Settings › Import sessions has a stream in flight, even though that panel
// is no longer mounted. The row reads the shared import store.

import { act, cleanup, screen } from "@testing-library/react";

import { useSessionImportStore } from "@/store/sessionImport";

vi.mock("@/hooks/useConversations", async () => {
  const { conversationHooksMock } = await import("@/test/sidebarMockHelpers");
  return conversationHooksMock();
});

vi.mock("@/components/PermissionsModal", () => ({ PermissionsModal: () => null }));

import { useConversations } from "@/hooks/useConversations";

const useConvMock = vi.mocked(useConversations);

beforeEach(() => {
  useConvMock.mockImplementation(() => conversationPage([conv("conv_1")]));
  useSessionImportStore.setState({ importing: false, streamed: [], result: null, error: null });
});

afterEach(cleanup);

describe("sidebar import progress", () => {
  it("shows a status row in the session list only while an import is running", () => {
    renderSidebar();
    expect(screen.queryByTestId("sidebar-import-progress")).toBeNull();

    act(() =>
      useSessionImportStore.setState({ importing: true, streamed: [{ id: "c1", title: "One" }] }),
    );
    const status = screen.getByTestId("sidebar-import-progress");
    expect(screen.getByTestId("sidebar-conversation-list")).toContainElement(status);
    expect(status).toHaveTextContent("Importing sessions, 1 so far…");
    // Existing rows stay listed beneath the status.
    expect(screen.getByText("conv_1")).toBeInTheDocument();

    act(() => useSessionImportStore.setState({ importing: false }));
    expect(screen.queryByTestId("sidebar-import-progress")).toBeNull();
  });
});
