/**
 * React hook for Angela — the autonomous coding agent (sandbox).
 *
 * Backed by `/api/ai/workspaces/<workspaceId>/angela/...`
 * (see ai/angela/api.py). All calls are plain `fetch` with the Django
 * session cookie + CSRF header, mirroring the other ai/ hooks.
 *
 * Surfaces:
 *   - targets()                 allow-listed sandbox repos + deploy modes
 *   - listRuns({ issueId })     recent runs (optionally for one issue)
 *   - startRun(payload)         launch a run with a chosen deploy mode
 *   - getRun(runId)             run detail + ordered step feed (for polling)
 *   - approveProd(runId)        [staging_gate] approve → prod deploy
 *   - manualDeploy(runId)       [manual] ship a green artifact now
 *   - cancelRun(runId)
 *   - generateDocs({ target })  docs → (local) MediaWiki
 */

import { useCallback } from "react";

import { csrfHeaders } from "./csrf";

export type AngelaDeployMode = "staging_gate" | "autonomous_prod" | "manual";

export type AngelaStep = {
  id: string;
  phase: "plan" | "code" | "review" | "test" | "deploy" | "docs";
  status: "started" | "ok" | "failed" | "skipped";
  title: string;
  detail: string;
  iteration: number;
  created_at: string;
};

export type AngelaRun = {
  id: string;
  workspace_id: string;
  project_id: string | null;
  issue_id: string | null;
  parent_run_id: string | null;
  title: string;
  target_repo: string;
  prompt: string;
  deploy_mode: AngelaDeployMode;
  status:
    | "queued"
    | "coding"
    | "reviewing"
    | "testing"
    | "deploying"
    | "awaiting_approval"
    | "succeeded"
    | "failed"
    | "cancelled";
  branch: string;
  review_verdict: "pending" | "approved" | "changes_requested";
  test_passed: boolean | null;
  test_summary: string;
  iterations: number;
  deploy_target: string;
  deploy_url: string;
  wiki_url: string;
  error: string;
  created_at: string;
  updated_at: string;
  steps?: AngelaStep[];
};

export type AngelaTargets = {
  targets: string[];
  default_target: string;
  deploy_modes: { key: AngelaDeployMode; label: string }[];
};

export type StartRunPayload = {
  prompt?: string;
  issueId?: string;
  projectId?: string;
  target?: string;
  deployMode: AngelaDeployMode;
};

const TERMINAL: AngelaRun["status"][] = ["succeeded", "failed", "cancelled"];

export function isTerminal(status: AngelaRun["status"]): boolean {
  return TERMINAL.includes(status);
}

async function jsonGet<T>(url: string): Promise<T> {
  const r = await fetch(url, { credentials: "same-origin" });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error((body as { error?: string }).error || `HTTP ${r.status}`);
  return body as T;
}

async function jsonPost<T>(url: string, payload?: unknown): Promise<T> {
  const headers = await csrfHeaders({ "Content-Type": "application/json" });
  const r = await fetch(url, {
    method: "POST",
    credentials: "same-origin",
    headers,
    body: JSON.stringify(payload ?? {}),
  });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error((body as { error?: string }).error || `HTTP ${r.status}`);
  return body as T;
}

export function useAngela(workspaceId: string | undefined) {
  const base = workspaceId ? `/api/ai/workspaces/${workspaceId}/angela` : null;

  const targets = useCallback(async (): Promise<AngelaTargets> => {
    if (!base) throw new Error("no workspace");
    return jsonGet<AngelaTargets>(`${base}/targets/`);
  }, [base]);

  const listRuns = useCallback(
    async (opts?: { issueId?: string }): Promise<AngelaRun[]> => {
      if (!base) throw new Error("no workspace");
      const q = opts?.issueId ? `?issue_id=${encodeURIComponent(opts.issueId)}` : "";
      const body = await jsonGet<{ runs: AngelaRun[] }>(`${base}/runs/${q}`);
      return body.runs;
    },
    [base]
  );

  const startRun = useCallback(
    async (p: StartRunPayload): Promise<AngelaRun> => {
      if (!base) throw new Error("no workspace");
      return jsonPost<AngelaRun>(`${base}/runs/`, {
        prompt: p.prompt,
        issue_id: p.issueId,
        project_id: p.projectId,
        target: p.target,
        deploy_mode: p.deployMode,
      });
    },
    [base]
  );

  const getRun = useCallback(
    async (runId: string): Promise<AngelaRun> => {
      if (!base) throw new Error("no workspace");
      return jsonGet<AngelaRun>(`${base}/runs/${runId}/`);
    },
    [base]
  );

  const approveProd = useCallback(
    async (runId: string) => {
      if (!base) throw new Error("no workspace");
      return jsonPost<{ status: string }>(`${base}/runs/${runId}/approve/`);
    },
    [base]
  );

  const manualDeploy = useCallback(
    async (runId: string) => {
      if (!base) throw new Error("no workspace");
      return jsonPost<{ status: string }>(`${base}/runs/${runId}/deploy/`);
    },
    [base]
  );

  const cancelRun = useCallback(
    async (runId: string) => {
      if (!base) throw new Error("no workspace");
      return jsonPost<AngelaRun>(`${base}/runs/${runId}/cancel/`);
    },
    [base]
  );

  const generateDocs = useCallback(
    async (opts?: { target?: string; projectId?: string }): Promise<AngelaRun> => {
      if (!base) throw new Error("no workspace");
      return jsonPost<AngelaRun>(`${base}/docs/`, {
        target: opts?.target,
        project_id: opts?.projectId,
      });
    },
    [base]
  );

  const refine = useCallback(
    async (runId: string, prompt: string): Promise<AngelaRun> => {
      if (!base) throw new Error("no workspace");
      return jsonPost<AngelaRun>(`${base}/runs/${runId}/refine/`, { prompt });
    },
    [base]
  );

  const downloadUrl = useCallback(
    (runId: string): string => (base ? `${base}/runs/${runId}/download/` : "#"),
    [base]
  );

  return {
    targets,
    listRuns,
    startRun,
    getRun,
    approveProd,
    manualDeploy,
    cancelRun,
    generateDocs,
    refine,
    downloadUrl,
  };
}
