// Reconnect backoff shared by the session-updates WebSocket
// (sessionUpdatesSocket.ts) and the chat SSE pump (chatStore.ts).
//
// A visible page ramps 250 ms → 5 s with ±50% jitter so tabs reconnecting after
// a server blip don't synchronize into a thundering herd. A hidden page has
// nothing on screen to refresh, so every retry it schedules waits 45–60 s: a
// backgrounded tab or mobile WebView facing an unreachable server wakes the
// radio about once a minute instead of every few seconds.

export const RECONNECT_BASE_MS = 250;
export const RECONNECT_MAX_MS = 5_000;
export const HIDDEN_RECONNECT_MS = 60_000;
// Spread hidden retries over the last quarter of the window so tabs that were
// hidden together don't redial together.
export const HIDDEN_RECONNECT_JITTER_MS = 15_000;

/** Whether the document is hidden (always `false` outside a DOM). */
export function isPageHidden(): boolean {
  return typeof document !== "undefined" && document.hidden;
}

/**
 * Delay before the next reconnect. Visible pages back off from the base,
 * doubling per failure up to the cap; while `hidden`, every retry waits the
 * stretched cadence.
 *
 * @param failedAttempts - Consecutive failed opens so far, starting at 1.
 * @param hidden - Whether the page is hidden; defaults to the live document state.
 * @returns The jittered delay in milliseconds.
 */
export function nextReconnectDelay(failedAttempts: number, hidden = isPageHidden()): number {
  if (hidden) return HIDDEN_RECONNECT_MS - Math.random() * HIDDEN_RECONNECT_JITTER_MS;
  const base = Math.min(RECONNECT_BASE_MS * 2 ** (failedAttempts - 1), RECONNECT_MAX_MS);
  return base / 2 + Math.random() * (base / 2);
}

/**
 * Wait out the next reconnect delay. Ends early when `signal` aborts and, for
 * a wait scheduled while hidden, as soon as the page becomes visible, so a
 * foregrounded page reconnects at once instead of sitting out the stretch.
 *
 * @param failedAttempts - Consecutive failed opens so far, starting at 1.
 * @param signal - Aborting it ends the wait immediately.
 * @returns Resolves when the caller should reconnect.
 */
export function awaitReconnectDelay(failedAttempts: number, signal: AbortSignal): Promise<void> {
  const hidden = isPageHidden();
  return new Promise((resolve) => {
    const finish = (): void => {
      clearTimeout(timer);
      signal.removeEventListener("abort", finish);
      if (hidden) document.removeEventListener("visibilitychange", onVisibilityChange);
      resolve();
    };
    const onVisibilityChange = (): void => {
      if (!document.hidden) finish();
    };
    const timer = setTimeout(finish, nextReconnectDelay(failedAttempts, hidden));
    signal.addEventListener("abort", finish, { once: true });
    if (hidden) document.addEventListener("visibilitychange", onVisibilityChange);
    // Register first, then re-check: an abort that fired before the listener
    // was attached never dispatches to it.
    if (signal.aborted) finish();
  });
}
