/**
 * React hook for the `/api/ai/workspaces/<id>/index-status/` endpoint
 * (TZ 1.8). Fetches once on mount, optionally polls while the index
 * is still filling.
 *
 * Polling discipline: callers pass a desired poll interval, but the
 * hook itself stops polling automatically once ``ready === true``.
 * That way the page doesn't hammer the endpoint forever once the
 * backfill is done; the next time the user navigates back, the
 * one-shot fetch on mount picks up the current state.
 */

import { useCallback, useEffect, useRef, useState } from "react";

export type IndexStatusBreakdown = Record<
  "work_item" | "comment" | "page",
  { total: number; indexed: number; coverage: number }
>;

export type IndexStatus = {
  workspace_id: string;
  total: number;
  indexed: number;
  /** 0..1, rounded to 2 decimals server-side */
  coverage: number;
  /** coverage >= 0.95 — frontend can unlock the search UI */
  ready: boolean;
  by_source: IndexStatusBreakdown;
};

export type UseIndexStatusResult = {
  data: IndexStatus | null;
  error: string | null;
  loading: boolean;
  /** Force a fresh fetch (e.g. after user kicked a manual backfill) */
  refresh: () => Promise<void>;
};

export function useIndexStatus(
  workspaceId: string,
  /**
   * Polling interval in milliseconds. The hook stops the timer once
   * `ready=true` regardless of this value. Pass 0 (default) for a
   * single fetch on mount.
   */
  pollMs: number = 0
): UseIndexStatusResult {
  const [data, setData] = useState<IndexStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const aliveRef = useRef(true);

  const fetchOnce = useCallback(async () => {
    setLoading(true);
    try {
      const resp = await fetch(
        `/api/ai/workspaces/${workspaceId}/index-status/`,
        { credentials: "same-origin" }
      );
      if (!resp.ok) {
        const body = await resp.json().catch(() => ({}));
        if (!aliveRef.current) return;
        setError(body.error || `HTTP ${resp.status}`);
        return;
      }
      const json = (await resp.json()) as IndexStatus;
      if (!aliveRef.current) return;
      setData(json);
      setError(null);
    } catch (e) {
      if (!aliveRef.current) return;
      setError((e as Error).message || "network error");
    } finally {
      if (aliveRef.current) setLoading(false);
    }
  }, [workspaceId]);

  useEffect(() => {
    aliveRef.current = true;
    fetchOnce();
    return () => {
      aliveRef.current = false;
    };
  }, [fetchOnce]);

  useEffect(() => {
    // Polling: only run while data exists, ready is false, and a
    // positive interval was requested. Stop on ready=true to avoid
    // the perpetual-polling trap.
    if (pollMs <= 0) return;
    if (data?.ready) return;
    const timer = setInterval(() => {
      fetchOnce();
    }, pollMs);
    return () => clearInterval(timer);
  }, [pollMs, data?.ready, fetchOnce]);

  return { data, error, loading, refresh: fetchOnce };
}
