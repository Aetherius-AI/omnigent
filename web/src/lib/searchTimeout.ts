import { ApiError } from "@/lib/sessionsApi";

/** Server error code for a statement the database cancelled at its time limit. */
export const QUERY_TIMEOUT_CODE = "query_timeout";

/** True for the DOMException a fetch raises when its `AbortSignal.timeout` fires. */
export function isAbortTimeout(error: unknown): boolean {
  return error instanceof DOMException && error.name === "TimeoutError";
}

/**
 * A search that ended at a deadline (the client's fetch timeout or the server's
 * `query_timeout`) rather than failing: not retried, and shown as timed out.
 */
export function isSearchTimeoutError(error: unknown): boolean {
  return isAbortTimeout(error) || (error instanceof ApiError && error.code === QUERY_TIMEOUT_CODE);
}
