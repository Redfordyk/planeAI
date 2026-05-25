/**
 * Hooks for the orchestrator REST endpoints (phases 7-12).
 *
 *   useGoals(workspaceId)               — GET /goals/
 *   useCreateGoal(workspaceId)          — POST /goals/ (runs PLANNER inline)
 *   useApplyPlan(workspaceId, goalId)   — POST /goals/<id>/apply/
 *   useActivityFeed(workspaceId)        — GET /actions/
 *   useRisks(workspaceId)               — GET /risks/
 *   useKillSwitch(workspaceId)          — GET + POST /kill-switch/
 *
 * Every POST attaches X-CSRFToken via csrfHeaders(). Errors return
 * `{ ok: false, error: string }` so the UI never crashes on a 400.
 */

import { useCallback, useEffect, useState } from "react";

import { csrfHeaders } from "./csrf";

const base = (workspaceId: string) =>
  `/api/ai/workspaces/${workspaceId}/orchestrator`;

export type Goal = {
  id: string;
  workspace_id: string;
  project_id: string | null;
  title: string;
  description: string;
  deadline: string | null;
  status: string;
  plan_preview: any;
  plan_issue_count: number;
  plan_issue_ids: string[];
  created_at: string;
  updated_at: string;
};

export type Action = {
  id: string;
  agent_type: string;
  action_type: string;
  status: string;
  risk_level: string;
  reasoning: string;
  input: any;
  output: any;
  project_id: string | null;
  goal_id: string | null;
  target_issue_id: string | null;
  created_at: string;
};

export type Risk = {
  id: string;
  issue_id: string;
  project_id: string;
  risk_type: string;
  impact: string;
  confidence: number;
  rationale: string;
  suggested_actions: { id: string; label: string; cost: string }[];
  resolved: boolean;
  escalated_at: string | null;
  created_at: string;
};

async function getJson<T>(url: string): Promise<T | null> {
  try {
    const r = await fetch(url, { credentials: "same-origin" });
    if (!r.ok) return null;
    return (await r.json()) as T;
  } catch {
    return null;
  }
}

async function postJson<T>(url: string, body: any): Promise<{ ok: boolean; data?: T; error?: string }> {
  try {
    const headers = await csrfHeaders({ "Content-Type": "application/json" });
    const r = await fetch(url, {
      method: "POST",
      headers,
      body: JSON.stringify(body),
      credentials: "same-origin",
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) return { ok: false, error: data?.error || `HTTP ${r.status}` };
    return { ok: true, data: data as T };
  } catch (e: any) {
    return { ok: false, error: e?.message || "network error" };
  }
}

export function useGoals(workspaceId: string) {
  const [goals, setGoals] = useState<Goal[] | null>(null);
  const [loading, setLoading] = useState(false);

  const reload = useCallback(async () => {
    if (!workspaceId) return;
    setLoading(true);
    const r = await getJson<{ goals: Goal[] }>(`${base(workspaceId)}/goals/`);
    setGoals(r?.goals ?? []);
    setLoading(false);
  }, [workspaceId]);

  useEffect(() => {
    reload();
  }, [reload]);

  return { goals, loading, reload };
}

export function useCreateGoal(workspaceId: string) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const create = useCallback(
    async (input: {
      title: string;
      description?: string;
      deadline?: string;
      project?: string;
      run_planner?: boolean;
      constraints?: Record<string, any>;
    }): Promise<{ ok: boolean; goal?: Goal; plan_summary?: any; error?: string }> => {
      setBusy(true);
      setErr(null);
      const res = await postJson<{ goal: Goal; plan_summary: any }>(
        `${base(workspaceId)}/goals/`,
        input
      );
      setBusy(false);
      if (!res.ok) {
        setErr(res.error ?? "error");
        return { ok: false, error: res.error };
      }
      return { ok: true, goal: res.data?.goal, plan_summary: res.data?.plan_summary };
    },
    [workspaceId]
  );

  return { create, busy, err };
}

export function useApplyPlan(workspaceId: string) {
  const [busy, setBusy] = useState(false);
  const apply = useCallback(
    async (goalId: string, project?: string) => {
      setBusy(true);
      const res = await postJson<{ applied: any; goal: Goal }>(
        `${base(workspaceId)}/goals/${goalId}/apply/`,
        project ? { project } : {}
      );
      setBusy(false);
      return res;
    },
    [workspaceId]
  );
  return { apply, busy };
}

export function useActivityFeed(workspaceId: string, agent?: string) {
  const [actions, setActions] = useState<Action[]>([]);
  const [loading, setLoading] = useState(false);
  const reload = useCallback(async () => {
    if (!workspaceId) return;
    setLoading(true);
    const url = agent
      ? `${base(workspaceId)}/actions/?agent=${agent}`
      : `${base(workspaceId)}/actions/`;
    const r = await getJson<{ actions: Action[] }>(url);
    setActions(r?.actions ?? []);
    setLoading(false);
  }, [workspaceId, agent]);
  useEffect(() => {
    reload();
  }, [reload]);
  return { actions, loading, reload };
}

export function useRisks(workspaceId: string) {
  const [risks, setRisks] = useState<Risk[]>([]);
  const [loading, setLoading] = useState(false);
  const reload = useCallback(async () => {
    if (!workspaceId) return;
    setLoading(true);
    const r = await getJson<{ risks: Risk[] }>(`${base(workspaceId)}/risks/`);
    setRisks(r?.risks ?? []);
    setLoading(false);
  }, [workspaceId]);
  useEffect(() => {
    reload();
  }, [reload]);
  const resolve = useCallback(
    async (riskId: string) => {
      const r = await postJson<{ risk: Risk }>(
        `${base(workspaceId)}/risks/${riskId}/resolve/`,
        {}
      );
      if (r.ok) reload();
      return r;
    },
    [workspaceId, reload]
  );
  return { risks, loading, reload, resolve };
}

export function useKillSwitch(workspaceId: string) {
  const [engaged, setEngaged] = useState<boolean | null>(null);
  const reload = useCallback(async () => {
    const r = await getJson<{ engaged: boolean }>(`${base(workspaceId)}/kill-switch/`);
    setEngaged(r?.engaged ?? null);
  }, [workspaceId]);
  useEffect(() => {
    reload();
  }, [reload]);
  const flip = useCallback(
    async (next: boolean) => {
      const r = await postJson<{ engaged: boolean }>(
        `${base(workspaceId)}/kill-switch/`,
        { engaged: next }
      );
      if (r.ok) setEngaged(r.data?.engaged ?? next);
      return r;
    },
    [workspaceId]
  );
  return { engaged, flip, reload };
}

export type ProjectLite = { id: string; name: string; identifier: string };

export function useProjects(workspaceSlug: string) {
  const [projects, setProjects] = useState<ProjectLite[]>([]);
  const [loading, setLoading] = useState(false);
  const reload = useCallback(async () => {
    if (!workspaceSlug) return;
    setLoading(true);
    const r = await getJson<ProjectLite[]>(`/api/workspaces/${workspaceSlug}/projects/`);
    setProjects(Array.isArray(r) ? r : []);
    setLoading(false);
  }, [workspaceSlug]);
  useEffect(() => {
    reload();
  }, [reload]);
  return { projects, loading, reload };
}

export function useTriggerScan(workspaceId: string) {
  const [busy, setBusy] = useState(false);
  const run = useCallback(
    async (projectId: string) => {
      setBusy(true);
      const r = await postJson(
        `${base(workspaceId)}/trigger/scan/`,
        { project_id: projectId }
      );
      setBusy(false);
      return r;
    },
    [workspaceId]
  );
  return { run, busy };
}
