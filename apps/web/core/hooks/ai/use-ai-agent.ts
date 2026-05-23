/**
 * Hook for the AI agent endpoint
 * (POST /api/ai/workspaces/<id>/agent/execute/).
 *
 * Unlike search (SSE streaming), the agent does multi-step tool-use
 * server-side and returns one JSON blob with the final reply plus an
 * action log. We expose loading + result + error state.
 */

import { useCallback, useState } from "react";

import { csrfHeaders } from "./csrf";

export type AgentAction = {
  tool: string;
  args: Record<string, unknown>;
  result: Record<string, unknown>;
  ok: boolean;
};

export type AgentResult = {
  reply: string;
  actions: AgentAction[];
  turns: number;
  total_cost_usd: string;
};

export type UseAIAgentResult = {
  result: AgentResult | null;
  loading: boolean;
  error: string | null;
  run: (prompt: string) => Promise<void>;
  reset: () => void;
};

export function useAIAgent(workspaceId: string): UseAIAgentResult {
  const [result, setResult] = useState<AgentResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const reset = useCallback(() => {
    setResult(null);
    setError(null);
  }, []);

  const run = useCallback(
    async (prompt: string) => {
      const text = prompt.trim();
      if (!text) return;
      setLoading(true);
      setError(null);
      setResult(null);
      try {
        const headers = await csrfHeaders({
          "Content-Type": "application/json",
        });
        const resp = await fetch(
          `/api/ai/workspaces/${workspaceId}/agent/execute/`,
          {
            method: "POST",
            headers,
            credentials: "same-origin",
            body: JSON.stringify({ prompt: text }),
          }
        );
        if (!resp.ok) {
          const body = await resp.json().catch(() => ({}));
          setError(body.error || `HTTP ${resp.status}`);
          return;
        }
        const json = (await resp.json()) as AgentResult;
        setResult(json);
      } catch (e) {
        setError((e as Error).message || "network error");
      } finally {
        setLoading(false);
      }
    },
    [workspaceId]
  );

  return { result, loading, error, run, reset };
}
