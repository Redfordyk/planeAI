/**
 * Per-project AI on/off toggle.
 *
 * Backed by `/api/ai/workspaces/<workspaceId>/projects/<projectId>/ai-settings/`
 * (see ai/views.py:ProjectAISettingsView). The server returns the
 * inverse-pair `ai_enabled` / `exclude_from_ai`; this hook surfaces
 * `aiEnabled` and stores the inverse server-side.
 *
 * New projects have no AIProjectSettings row, which means
 * `ai_enabled=true` by default per spec.
 */

import { useCallback, useEffect, useState } from "react";

import { csrfHeaders } from "./csrf";

type ProjectAISettings = {
  project_id: string;
  workspace_id: string;
  ai_enabled: boolean;
  exclude_from_ai: boolean;
};

export type UseProjectAISettings = {
  aiEnabled: boolean | null;
  loading: boolean;
  saving: boolean;
  error: string | null;
  setAIEnabled: (next: boolean) => Promise<void>;
};

export function useProjectAISettings(
  workspaceId: string | undefined,
  projectId: string | undefined
): UseProjectAISettings {
  const [aiEnabled, setAi] = useState<boolean | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [saving, setSaving] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  const url =
    workspaceId && projectId
      ? `/api/ai/workspaces/${workspaceId}/projects/${projectId}/ai-settings/`
      : null;

  useEffect(() => {
    if (!url) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    fetch(url, { credentials: "same-origin" })
      .then(async (r) => {
        const body = (await r.json().catch(() => ({}))) as Partial<ProjectAISettings> & {
          error?: string;
        };
        if (cancelled) return;
        if (!r.ok) {
          setError(body.error || `HTTP ${r.status}`);
          setAi(null);
          return;
        }
        setAi(Boolean(body.ai_enabled));
      })
      .catch((e) => {
        if (cancelled) return;
        setError(String(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [url]);

  const setAIEnabled = useCallback(
    async (next: boolean) => {
      if (!url) return;
      const prev = aiEnabled;
      setAi(next); // optimistic
      setSaving(true);
      setError(null);
      try {
        const headers = await csrfHeaders({ "Content-Type": "application/json" });
        const resp = await fetch(url, {
          method: "PATCH",
          credentials: "same-origin",
          headers,
          body: JSON.stringify({ ai_enabled: next }),
        });
        const body = (await resp.json().catch(() => ({}))) as Partial<ProjectAISettings> & {
          error?: string;
        };
        if (!resp.ok) {
          setAi(prev); // rollback
          setError(body.error || `HTTP ${resp.status}`);
          return;
        }
        setAi(Boolean(body.ai_enabled));
      } catch (e) {
        setAi(prev);
        setError(String(e));
      } finally {
        setSaving(false);
      }
    },
    [aiEnabled, url]
  );

  return { aiEnabled, loading, saving, error, setAIEnabled };
}
