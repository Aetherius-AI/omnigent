import { act, cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { SessionImportStatus } from "./SessionImportStatus";
import { useSessionImportStore } from "@/store/sessionImport";

function renderStatus() {
  return render(
    <MemoryRouter>
      <SessionImportStatus />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  useSessionImportStore.setState({ importing: false, streamed: [], result: null, error: null });
});

afterEach(cleanup);

describe("SessionImportStatus", () => {
  it("renders nothing while no import is running", () => {
    renderStatus();
    expect(screen.queryByTestId("sidebar-import-progress")).toBeNull();
  });

  it("shows the running import with its count, links to the panel, and clears when done", () => {
    act(() => useSessionImportStore.setState({ importing: true, streamed: [] }));
    renderStatus();
    const status = screen.getByTestId("sidebar-import-progress");
    expect(status).toHaveTextContent("Importing sessions…");
    expect(screen.getByRole("link", { name: /Importing sessions/ })).toHaveAttribute(
      "href",
      "/settings/import",
    );

    act(() =>
      useSessionImportStore.setState({
        streamed: [
          { id: "c1", title: "One" },
          { id: "c2", title: null },
          { id: "c3", title: "Three" },
        ],
      }),
    );
    expect(status).toHaveTextContent("Importing sessions, 3 so far…");

    act(() => useSessionImportStore.setState({ importing: false }));
    expect(screen.queryByTestId("sidebar-import-progress")).toBeNull();
  });
});
