// Module-scope store for the in-flight local session import, so the sidebar
// can show it and the Settings panel can pick it back up after a remount.

import type { QueryClient } from "@tanstack/react-query";
import { create } from "zustand";
import {
  importLocalSessions,
  type ImportedSessionRef,
  type ImportSourceSelector,
  type LocalImportResult,
} from "@/lib/sessionsApi";

export interface SessionImportRequest {
  hostId: string;
  source: ImportSourceSelector;
  limit: number;
  /** Import one known harness session instead of the recent ones. */
  sessionId?: string;
}

interface SessionImportState {
  /** True from the Import click until the stream settles with a result or error. */
  importing: boolean;
  /** Sessions appended as their frames stream in, so lists fill live. */
  streamed: ImportedSessionRef[];
  result: LocalImportResult | null;
  error: string | null;
  /** Run one import; resolves when the stream settles. Ignored while one is in flight. */
  start: (request: SessionImportRequest, queryClient: QueryClient) => Promise<void>;
}

// The server persists each session as its frame arrives, so the sidebar list
// is refreshed on this cadence while the import is in flight.
const LIST_REFRESH_MS = 5000;

export const useSessionImportStore = create<SessionImportState>((set, get) => ({
  importing: false,
  streamed: [],
  result: null,
  error: null,
  start: async (request, queryClient) => {
    if (get().importing) return;
    set({ importing: true, streamed: [], result: null, error: null });
    const refreshList = () => void queryClient.invalidateQueries({ queryKey: ["conversations"] });
    const refresh = setInterval(refreshList, LIST_REFRESH_MS);
    try {
      const { hostId, source, limit, sessionId } = request;
      const onSession = (s: ImportedSessionRef) =>
        set((state) => ({ streamed: [...state.streamed, s] }));
      const result =
        sessionId !== undefined
          ? await importLocalSessions(hostId, source, limit, onSession, sessionId)
          : await importLocalSessions(hostId, source, limit, onSession);
      set({ result });
    } catch (e) {
      set({ error: e instanceof Error ? e.message : "Import failed. Try again." });
    } finally {
      clearInterval(refresh);
      set({ importing: false });
    }
    // Newly imported sessions land in the sidebar list.
    await queryClient.invalidateQueries({ queryKey: ["conversations"] });
  },
}));
