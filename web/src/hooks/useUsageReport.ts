import { useEffect } from "react";
import { useInfiniteQuery, useQueryClient } from "@tanstack/react-query";
import { fetchUsageReport, StaleCursorError } from "@/lib/usageApi";

export function useUsageReport(since: string | null, until: string | null) {
  const queryClient = useQueryClient();
  const query = useInfiniteQuery({
    queryKey: ["usage", since, until],
    queryFn: ({ pageParam }) =>
      fetchUsageReport({ limit: 50, after: pageParam ?? null, since, until }),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) =>
      lastPage.sessionsHasMore ? lastPage.sessionsLastId : undefined,
    staleTime: 60_000,
    // A stale cursor cannot be recovered by retrying the same page; let the
    // reset below restart from the first page instead of retrying blindly.
    retry: (failureCount, error) => !(error instanceof StaleCursorError) && failureCount < 3,
  });

  // A session the cursor pointed at was deleted between page fetches. The
  // loaded pages are now unanchored, so drop them and refetch the first page
  // rather than leaving Load More wedged on the dead cursor.
  const staleCursor = query.error instanceof StaleCursorError;
  useEffect(() => {
    if (staleCursor) {
      void queryClient.resetQueries({ queryKey: ["usage", since, until] });
    }
  }, [staleCursor, queryClient, since, until]);

  return query;
}
