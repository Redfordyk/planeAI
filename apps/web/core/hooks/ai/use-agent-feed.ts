/**
 * TZ 5.6 — hooks for the agent transparency UI.
 *
 * Three small hooks layered on the audit endpoints (see
 * ai/agent_views.py):
 *
 *   - useAgentFeed         GET /agent/actions/
 *   - useAgents            GET /agents/  +  PATCH /agents/<id>/
 *   - useIssuesTouched     GET /issues/touched/?ids=
 *
 * All endpoints are session-authenticated (`credentials: "same-origin"`,
 * same pattern as `useAISearch` from TZ 2.4). They return JSON only —
 * no SSE — so the hooks here are far simpler than the search hook.
 *
 * Pagination is page-number-based: the feed returns `{count, page,
 * page_size, results}`. The feed hook exposes a `setPage` helper that
 * keeps `data.results` rendered while the next page loads, so the UI
 * doesn't flash empty between page transitions.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

// ---------------------------------------------------------------------------
// Types — mirror agent_views.py serializers exactly
// ---------------------------------------------------------------------------

export type AgentActionStatus = "applied" | "rejected" | "error";

export type AgentAction = {
  id: string;
  agent_id: string;
  workspace_id: string;
  project_id: string;
  issue_id: string;
  tool_name: string;
  status: AgentActionStatus;
  input: Record<string, unknown>;
  output: Record<string, unknown>;
  error: string;
  /** Short human-readable label assembled by the backend. */
  rationale: string;
  created_at: string;
  undone_at: string | null;
  undone_by_id: string | null;
  /** True only for applied, not-undone, in REVERSIBLE_TOOLS. */
  reversible: boolean;
};

export type AgentActionPage = {
  results: AgentAction[];
  count: number;
  page: number;
  page_size: number;
};

export type AgentRow = {
  id: string;
  workspace_id: string;
  user_id: string;
  user_email: string;
  enabled: boolean;
  created_at: string;
  updated_at: string;
};

export type AgentFeedFilters = {
  project?: string | null;
  tool?: string | null;
  status?: AgentActionStatus | null;
  issue?: string | null;
  since?: string | null;
};

// ---------------------------------------------------------------------------
// useAgentFeed
// ---------------------------------------------------------------------------

export type UseAgentFeedResult = {
  data: AgentActionPage | null;
  error: string | null;
  loading: boolean;
  page: number;
  setPage: (n: number) => void;
  filters: AgentFeedFilters;
  setFilters: (f: AgentFeedFilters) => void;
  refresh: () => Promise<void>;
  /** Optimistic local update — call after a successful undo so the
   * feed row updates without waiting for a refetch. */
  patchAction: (id: string, patch: Partial<AgentAction>) => void;
};

function buildQuery(
  page: number,
  pageSize: number,
  filters: AgentFeedFilters
): string {
  const sp = new URLSearchParams();
  sp.set("page", String(page));
  sp.set("page_size", String(pageSize));
  if (filters.project) sp.set("project", filters.project);
  if (filters.tool) sp.set("tool", filters.tool);
  if (filters.status) sp.set("status", filters.status);
  if (filters.issue) sp.set("issue", filters.issue);
  if (filters.since) sp.set("since", filters.since);
  return sp.toString();
}

export function useAgentFeed(
  workspaceId: string,
  initialFilters: AgentFeedFilters = {},
  pageSize: number = 30
): UseAgentFeedResult {
  const [data, setData] = useState<AgentActionPage | null>(null);
  const [page, setPage] = useState(1);
  const [filters, setFilters] = useState<AgentFeedFilters>(initialFilters);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const aliveRef = useRef(true);

  const fetchOnce = useCallback(async () => {
    setLoading(true);
    try {
      const qs = buildQuery(page, pageSize, filters);
      const resp = await fetch(
        `/api/ai/workspaces/${workspaceId}/agent/actions/?${qs}`,
        { credentials: "same-origin" }
      );
      if (!resp.ok) {
        const body: { error?: string } = await resp.json().catch(() => ({}));
        if (!aliveRef.current) return;
        setError(body.error || `HTTP ${resp.status}`);
        return;
      }
      const json = (await resp.json()) as AgentActionPage;
      if (!aliveRef.current) return;
      setData(json);
      setError(null);
    } catch (e) {
      if (!aliveRef.current) return;
      setError((e as Error).message || "network error");
    } finally {
      if (aliveRef.current) setLoading(false);
    }
  }, [workspaceId, page, pageSize, filters]);

  useEffect(() => {
    aliveRef.current = true;
    fetchOnce();
    return () => {
      aliveRef.current = false;
    };
  }, [fetchOnce]);

  // Whenever filters change, reset to page 1. Without this a user
  // viewing page 4 of "all actions" who narrows to "tool=set_labels"
  // (which may have one row) would land on an empty page 4.
  useEffect(() => {
    setPage(1);
  }, [filters]);

  const patchAction = useCallback(
    (id: string, patch: Partial<AgentAction>) => {
      setData((cur) => {
        if (!cur) return cur;
        return {
          ...cur,
          results: cur.results.map((a) => (a.id === id ? { ...a, ...patch } : a)),
        };
      });
    },
    []
  );

  return {
    data,
    error,
    loading,
    page,
    setPage,
    filters,
    setFilters,
    refresh: fetchOnce,
    patchAction,
  };
}

// ---------------------------------------------------------------------------
// undoAction — fire-and-forget mutation against POST .../undo/
// ---------------------------------------------------------------------------

export async function undoAction(
  workspaceId: string,
  actionId: string
): Promise<AgentAction> {
  const resp = await fetch(
    `/api/ai/workspaces/${workspaceId}/agent/actions/${actionId}/undo/`,
    {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
    }
  );
  if (!resp.ok) {
    const body: { error?: string } = await resp.json().catch(() => ({}));
    throw new Error(body.error || `HTTP ${resp.status}`);
  }
  return (await resp.json()) as AgentAction;
}

// ---------------------------------------------------------------------------
// useAgents
// ---------------------------------------------------------------------------

export type UseAgentsResult = {
  agents: AgentRow[];
  loading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
  setEnabled: (agentId: string, enabled: boolean) => Promise<AgentRow>;
};

export function useAgents(workspaceId: string): UseAgentsResult {
  const [agents, setAgents] = useState<AgentRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const aliveRef = useRef(true);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const resp = await fetch(
        `/api/ai/workspaces/${workspaceId}/agents/`,
        { credentials: "same-origin" }
      );
      if (!resp.ok) {
        const body: { error?: string } = await resp.json().catch(() => ({}));
        if (!aliveRef.current) return;
        setError(body.error || `HTTP ${resp.status}`);
        return;
      }
      const json = (await resp.json()) as { results: AgentRow[] };
      if (!aliveRef.current) return;
      setAgents(json.results);
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
    refresh();
    return () => {
      aliveRef.current = false;
    };
  }, [refresh]);

  const setEnabled = useCallback(
    async (agentId: string, enabled: boolean): Promise<AgentRow> => {
      const resp = await fetch(
        `/api/ai/workspaces/${workspaceId}/agents/${agentId}/`,
        {
          method: "PATCH",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enabled }),
        }
      );
      if (!resp.ok) {
        const body: { error?: string } = await resp.json().catch(() => ({}));
        throw new Error(body.error || `HTTP ${resp.status}`);
      }
      const updated = (await resp.json()) as AgentRow;
      setAgents((cur) => cur.map((a) => (a.id === updated.id ? updated : a)));
      return updated;
    },
    [workspaceId]
  );

  return { agents, loading, error, refresh, setEnabled };
}

// ---------------------------------------------------------------------------
// useIssuesTouched — bulk badge lookup
// ---------------------------------------------------------------------------

export type TouchedMap = Record<string, boolean>;

/**
 * Resolves "did the agent do anything visible on this issue?" for a
 * batch of issue ids. Cheap-but-not-free; cache by stable key (the
 * sorted joined id list) so a re-render of the issue list with the
 * same ids doesn't refetch.
 */
export function useIssuesTouched(
  workspaceId: string,
  issueIds: string[]
): { touched: TouchedMap; loading: boolean; error: string | null } {
  const [touched, setTouched] = useState<TouchedMap>({});
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Stable key so the effect doesn't refetch when the parent passes
  // a fresh array with identical contents on every render.
  const key = useMemo(
    () => [...issueIds].sort().join(","),
    [issueIds]
  );

  useEffect(() => {
    if (!issueIds.length) {
      setTouched({});
      return;
    }
    let alive = true;
    setLoading(true);
    (async () => {
      try {
        const resp = await fetch(
          `/api/ai/workspaces/${workspaceId}/issues/touched/?ids=${encodeURIComponent(
            issueIds.join(",")
          )}`,
          { credentials: "same-origin" }
        );
        if (!resp.ok) {
          const body: { error?: string } = await resp.json().catch(() => ({}));
          if (!alive) return;
          setError(body.error || `HTTP ${resp.status}`);
          return;
        }
        const json = (await resp.json()) as { touched: TouchedMap };
        if (!alive) return;
        setTouched(json.touched);
        setError(null);
      } catch (e) {
        if (!alive) return;
        setError((e as Error).message || "network error");
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspaceId, key]);

  return { touched, loading, error };
}
