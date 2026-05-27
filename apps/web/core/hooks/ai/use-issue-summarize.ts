/**
 * React hook for the per-issue summarize endpoint.
 *
 * Consumes ``POST /api/ai/workspaces/<wid>/issues/<iid>/summarize/``
 * (see ai/views.py:SummarizeIssueView). SSE frame layout:
 *
 *   data: {"cached": true,  "summary": "...", "updated_at": "...", "model": "..."}   // single frame
 *   --- OR ---
 *   data: {"cached": false, "model": "..."}     // first
 *   data: {"sources": []}                       // claude_sse always emits this
 *   data: {"delta": "..."}                      // 0..N
 *   data: {"done": true, "usage": {...}}        // last
 *
 * On any HTTP-level error (403/429/404/400) the response is a single
 * JSON body, not SSE.
 *
 * Returns a status state machine the modal/UI can render directly.
 */

import { useCallback, useRef, useState } from "react";

import { csrfHeaders } from "./csrf";

export type SummarizeStatus = "idle" | "streaming" | "done" | "error";

export type UseIssueSummarizeResult = {
  summary: string;
  status: SummarizeStatus;
  /** True when the current `summary` came from the server-side cache. */
  cached: boolean;
  /** ISO datetime of the cached row, when `cached` is true. */
  updatedAt: string | null;
  /** Model that produced the summary. */
  model: string | null;
  error: string | null;
  /** Trigger a summarize. Pass `force: true` to bypass the content-hash cache. */
  summarize: (opts?: { force?: boolean }) => Promise<void>;
  /** Abort the in-flight stream, if any. */
  cancel: () => void;
};

export function useIssueSummarize(
  workspaceId: string | undefined,
  issueId: string | undefined
): UseIssueSummarizeResult {
  const [summary, setSummary] = useState<string>("");
  const [status, setStatus] = useState<SummarizeStatus>("idle");
  const [cached, setCached] = useState<boolean>(false);
  const [updatedAt, setUpdatedAt] = useState<string | null>(null);
  const [model, setModel] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const cancel = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
  }, []);

  const summarize = useCallback(
    async (opts: { force?: boolean } = {}) => {
      if (!workspaceId || !issueId) return;

      setSummary("");
      setCached(false);
      setUpdatedAt(null);
      setModel(null);
      setError(null);
      setStatus("streaming");

      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;

      let resp: Response;
      try {
        const headers = await csrfHeaders({ "Content-Type": "application/json" });
        resp = await fetch(
          `/api/ai/workspaces/${workspaceId}/issues/${issueId}/summarize/`,
          {
            method: "POST",
            headers,
            body: JSON.stringify({ force: !!opts.force }),
            credentials: "same-origin",
            signal: controller.signal,
          }
        );
      } catch (e) {
        if ((e as DOMException)?.name === "AbortError") {
          setStatus("idle");
          return;
        }
        setError((e as Error).message || "network error");
        setStatus("error");
        return;
      }

      if (!resp.ok) {
        let body: { error?: string } = {};
        try {
          body = await resp.json();
        } catch {
          // not JSON
        }
        setError(body.error || `HTTP ${resp.status}`);
        setStatus("error");
        return;
      }

      const reader = resp.body?.getReader();
      if (!reader) {
        setError("response body is not readable");
        setStatus("error");
        return;
      }

      const decoder = new TextDecoder();
      let buffer = "";
      let stop = false;

      const handleFrame = (raw: string): boolean => {
        if (!raw.startsWith("data: ")) return false;
        let payload: Record<string, unknown>;
        try {
          payload = JSON.parse(raw.slice("data: ".length));
        } catch {
          return false;
        }
        // First frame: cache hit/miss header.
        if (typeof payload.cached === "boolean") {
          setCached(payload.cached);
          if (typeof payload.model === "string") setModel(payload.model);
          if (typeof payload.updated_at === "string") setUpdatedAt(payload.updated_at);
          if (payload.cached && typeof payload.summary === "string") {
            setSummary(payload.summary);
          }
          return false;
        }
        if (Array.isArray(payload.sources)) {
          // claude_sse emits an empty sources frame — ignore for summarize.
          return false;
        }
        if (typeof payload.delta === "string") {
          setSummary((prev) => prev + (payload.delta as string));
          return false;
        }
        if (typeof payload.error === "string") {
          setError(payload.error);
          setStatus("error");
          return true;
        }
        if (payload.done === true) {
          return true;
        }
        return false;
      };

      try {
        // eslint-disable-next-line no-constant-condition
        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          let nlIdx;
          // Each SSE frame is terminated by a blank line ("\n\n").
          while ((nlIdx = buffer.indexOf("\n\n")) !== -1) {
            const rawFrame = buffer.slice(0, nlIdx);
            buffer = buffer.slice(nlIdx + 2);
            stop = handleFrame(rawFrame) || stop;
            if (stop) break;
          }
          if (stop) break;
        }
      } catch (e) {
        if ((e as DOMException)?.name === "AbortError") {
          setStatus("idle");
          return;
        }
        setError((e as Error).message || "stream error");
        setStatus("error");
        return;
      }

      if (!stop || (!summary && !abortRef.current)) {
        // Normal completion without an explicit done frame — still ok.
      }
      setStatus((prev) => (prev === "error" ? prev : "done"));
    },
    // We intentionally leave `summary` out of deps; the closure reads it
    // only via setSummary updater pattern.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [workspaceId, issueId]
  );

  return {
    summary,
    status,
    cached,
    updatedAt,
    model,
    error,
    summarize,
    cancel,
  };
}
