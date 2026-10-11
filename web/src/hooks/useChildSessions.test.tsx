import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { PropsWithChildren } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { authenticatedFetch } from "@/lib/identity";
import {
  childSessionsQueryKey,
  type ChildSessionInfo,
  useChildSessions,
  useDescendantSession,
  useSubagentTree,
} from "./useChildSessions";

vi.mock("@/lib/identity", () => ({
  authenticatedFetch: vi.fn(),
}));

function wrapper({ children }: PropsWithChildren) {
  return (
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      {children}
    </QueryClientProvider>
  );
}

beforeEach(() => {
  vi.mocked(authenticatedFetch).mockReset();
});

/** A minimal ``descendants`` wire row under ``parentId`` at ``depth``. */
function descendantRow(id: string, parentId: string, depth: number) {
  return {
    id,
    parent_session_id: parentId,
    depth,
    title: `worker:${id}`,
    tool: "worker",
    session_name: id,
    current_task_status: null,
    busy: false,
  };
}

function jsonResponse(body: unknown, init?: ResponseInit): Response {
  return new Response(JSON.stringify(body), init);
}

function ids(rows: ChildSessionInfo[] | undefined): string[] | undefined {
  return rows?.map((row) => row.id);
}

function fetchedUrls(): unknown[] {
  return vi.mocked(authenticatedFetch).mock.calls.map(([url]) => url);
}

describe("useChildSessions", () => {
  it("does not fetch child sessions for a provisional conversation", () => {
    const { result } = renderHook(() => useChildSessions("temp:pending-create"), { wrapper });

    expect(result.current.children).toEqual([]);
    expect(authenticatedFetch).not.toHaveBeenCalled();
  });
});

describe("useSubagentTree", () => {
  it("loads the whole subtree in one request and seeds every parent's child list", async () => {
    // WHY: the Agents rail used to issue one child_sessions request per
    // rendered node; the subtree fetch must leave each row's list cached.
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const treeWrapper = ({ children }: PropsWithChildren) => (
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    );
    vi.mocked(authenticatedFetch).mockResolvedValueOnce(
      jsonResponse({
        object: "list",
        data: [
          descendantRow("conv_a", "conv_root", 1),
          descendantRow("conv_b", "conv_root", 1),
          descendantRow("conv_a1", "conv_a", 2),
          descendantRow("conv_a1x", "conv_a1", 3),
        ],
        last_id: "conv_a1x",
        has_more: false,
      }),
    );

    const { result } = renderHook(() => useSubagentTree("conv_root"), { wrapper: treeWrapper });

    expect(result.current.isLoading).toBe(true);
    await waitFor(() => expect(ids(result.current.children)).toEqual(["conv_a", "conv_b"]));
    expect(fetchedUrls()).toEqual(["/v1/sessions/conv_root/descendants?max_depth=3&limit=1000"]);
    expect(ids(queryClient.getQueryData(childSessionsQueryKey("conv_a")))).toEqual(["conv_a1"]);
    expect(queryClient.getQueryData(childSessionsQueryKey("conv_b"))).toEqual([]);
    expect(ids(queryClient.getQueryData(childSessionsQueryKey("conv_a1")))).toEqual(["conv_a1x"]);
    // Depth-cap rows render no child list, so nothing is seeded for them.
    expect(queryClient.getQueryData(childSessionsQueryKey("conv_a1x"))).toBeUndefined();
  });

  it("follows the page cursor to the end of a wide subtree", async () => {
    vi.mocked(authenticatedFetch)
      .mockResolvedValueOnce(
        jsonResponse({
          object: "list",
          data: [descendantRow("conv_a", "conv_root", 1)],
          last_id: "conv_a",
          has_more: true,
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          object: "list",
          data: [descendantRow("conv_b", "conv_root", 1)],
          last_id: "conv_b",
          has_more: false,
        }),
      );

    const { result } = renderHook(() => useSubagentTree("conv_root"), { wrapper });

    await waitFor(() => expect(ids(result.current.children)).toEqual(["conv_a", "conv_b"]));
    expect(fetchedUrls()).toEqual([
      "/v1/sessions/conv_root/descendants?max_depth=3&limit=1000",
      "/v1/sessions/conv_root/descendants?max_depth=3&limit=1000&after=conv_a",
    ]);
  });

  it("falls back to the per-node list when the server has no subtree endpoint", async () => {
    vi.mocked(authenticatedFetch)
      .mockResolvedValueOnce(new Response("Not Found", { status: 404, statusText: "Not Found" }))
      .mockResolvedValueOnce(
        jsonResponse({ object: "list", data: [descendantRow("conv_a", "conv_root", 1)] }),
      );

    const { result } = renderHook(() => useSubagentTree("conv_root"), { wrapper });

    await waitFor(() => expect(ids(result.current.children)).toEqual(["conv_a"]));
    expect(fetchedUrls()).toEqual([
      "/v1/sessions/conv_root/descendants?max_depth=3&limit=1000",
      "/v1/sessions/conv_root/child_sessions",
    ]);
  });

  it("does not fetch for a provisional conversation", () => {
    const { result } = renderHook(() => useSubagentTree("temp:pending-create"), { wrapper });

    expect(result.current.children).toEqual([]);
    expect(result.current.isLoading).toBe(false);
    expect(authenticatedFetch).not.toHaveBeenCalled();
  });
});

describe("useDescendantSession", () => {
  it("fetches the direct-child list when it is not cached", async () => {
    // WHY: a mirrored card can mount before anything loaded the ancestor's
    // child list; the lookup must fetch it rather than stay unlabeled.
    vi.mocked(authenticatedFetch).mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          object: "list",
          data: [
            {
              id: "conv_child",
              title: "researcher:auth",
              task_summary: "Investigate auth flow",
              tool: "researcher",
              session_name: "auth",
              current_task_status: "in_progress",
              busy: true,
            },
          ],
        }),
      ),
    );
    const { result } = renderHook(() => useDescendantSession("conv_parent", "conv_child"), {
      wrapper,
    });

    expect(result.current).toBeNull();
    await waitFor(() => expect(result.current?.task_summary).toBe("Investigate auth flow"));
    expect(authenticatedFetch).toHaveBeenCalledWith("/v1/sessions/conv_parent/child_sessions");
  });
});
