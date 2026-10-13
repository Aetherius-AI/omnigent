// Unit test for `useUsageReport` — a next-page cursor that goes stale must
// restart pagination from the first page instead of wedging the Usage page.

import { QueryClientProvider, QueryClient } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import * as usageApi from "@/lib/usageApi";
import { StaleCursorError, type UsageReport } from "@/lib/usageApi";
import { useUsageReport } from "./useUsageReport";

function makeReport(overrides: Partial<UsageReport>): UsageReport {
  return {
    costToday: 0,
    costLast7d: 0,
    costLast30d: 0,
    totalCostUsd: 0,
    dailyCosts: [],
    sessions: [],
    sessionsHasMore: false,
    sessionsLastId: null,
    harnessBreakdown: {},
    modelBreakdown: {},
    legacy: false,
    ...overrides,
  };
}

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return createElement(QueryClientProvider, { client }, children);
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("useUsageReport", () => {
  it("restarts from the first page when a next-page cursor goes stale", async () => {
    const fetchSpy = vi.spyOn(usageApi, "fetchUsageReport").mockImplementation(async (params) => {
      if (params?.after) throw new StaleCursorError();
      return makeReport({ sessionsHasMore: true, sessionsLastId: "conv_1" });
    });

    const { result } = renderHook(() => useUsageReport(null, null), { wrapper });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.hasNextPage).toBe(true);

    await act(async () => {
      await result.current.fetchNextPage();
    });

    // The stale cursor resets the query; the hook refetches page one (no
    // cursor) and stays usable rather than stuck in an error state.
    await waitFor(() => {
      expect(result.current.isError).toBe(false);
      expect(result.current.data?.pages).toHaveLength(1);
    });
    expect(fetchSpy.mock.calls.some(([params]) => params?.after === "conv_1")).toBe(true);
  });
});
