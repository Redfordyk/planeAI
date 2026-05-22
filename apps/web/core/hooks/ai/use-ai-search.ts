/**
 * React hook for the planeAI SSE search endpoint.
 *
 * Consumes `POST /api/ai/workspaces/<workspaceId>/search/` (TZ 2.3),
 * which streams Server-Sent Events in this shape:
 *
 *   data: {"sources":[{source_type, source_id, project_id}, ...]}
 *   data: {"delta": "text"}        // 0..N
 *   data: {"error": "..."}         // optional, no `done` follows
 *   data: {"done": true, "usage": {...}}
 *
 * The `sources` frame is always first — the UI component (TZ 2.6)
 * mounts the sources sidebar as soon as it arrives, before the
 * answer text starts streaming.
 *
 * HTTP-level errors (429 budget exceeded, 403 AI disabled, 400 empty
 * query) come back as normal JSON, not SSE, and surface as
 * `status: 'error'` with the message lifted out of the response body.
 */

import { useCallback, useRef, useState } from "react";

export type SearchSource = {
  source_type: "work_item" | "comment" | "page";
  source_id: string;
  project_id: string | null;
};

export type SearchStatus = "idle" | "streaming" | "done" | "error";

export type UseAISearchResult = {
  answer: string;
  sources: SearchSource[];
  status: SearchStatus;
  error: string | null;
  search: (query: string, opts?: { topK?: number }) => Promise<void>;
  cancel: () => void;
};

export function useAISearch(workspaceId: string): UseAISearchResult {
  const [answer, setAnswer] = useState("");
  const [sources, setSources] = useState<SearchSource[]>([]);
  const [status, setStatus] = useState<SearchStatus>("idle");
  const [error, setError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const cancel = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
  }, []);

  const search = useCallback(
    async (query: string, opts: { topK?: number } = {}) => {
      // Reset before the new request — keeps stale answer from
      // flashing while the next stream is starting.
      setAnswer("");
      setSources([]);
      setError(null);
      setStatus("streaming");

      // Cancel any in-flight stream from a previous search.
      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;

      let resp: Response;
      try {
        resp = await fetch(`/api/ai/workspaces/${workspaceId}/search/`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ query, top_k: opts.topK ?? 20 }),
          credentials: "same-origin",
          signal: controller.signal,
        });
      } catch (e) {
        // Network error / abort before the response arrived.
        if ((e as DOMException)?.name === "AbortError") {
          setStatus("idle");
          return;
        }
        setError((e as Error).message || "network error");
        setStatus("error");
        return;
      }

      if (!resp.ok) {
        // Pre-stream HTTP errors (budget 429, AI off 403, bad query 400)
        // arrive as a single JSON body, never as SSE frames.
        let body: { error?: string } = {};
        try {
          body = await resp.json();
        } catch {
          // body was not JSON — keep the status code as the message
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

      const handleFrame = (raw: string): boolean => {
        // `data: <json>` — strip the prefix. SSE spec also allows
        // `event:` and `:` (comment) lines; the backend never sends
        // them, so we treat anything else as noise.
        if (!raw.startsWith("data: ")) return false;
        let payload: Record<string, unknown>;
        try {
          payload = JSON.parse(raw.slice("data: ".length));
        } catch {
          return false;
        }
        if (Array.isArray(payload.sources)) {
          setSources(payload.sources as SearchSource[]);
          return false;
        }
        if (typeof payload.delta === "string") {
          setAnswer((prev) => prev + (payload.delta as string));
          return false;
        }
        if (typeof payload.error === "string") {
          setError(payload.error);
          setStatus("error");
          return true; // stop iterating
        }
        if (payload.done === true) {
          setStatus("done");
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
          // Frames are separated by a blank line per SSE spec. The
          // last element of split is the partially-read tail; keep it
          // for the next iteration — without this, frames straddling
          // a chunk boundary corrupt JSON parsing.
          const frames = buffer.split("\n\n");
          buffer = frames.pop() ?? "";
          for (const f of frames) {
            if (handleFrame(f.trim())) return;
          }
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

      // After the loop, flush any trailing payload that didn't end
      // with the SSE separator (some intermediaries strip the final
      // \n\n on connection close — the last `done` frame would be
      // lost without this).
      const tail = buffer.trim();
      if (tail) handleFrame(tail);

      // If we exit the loop without seeing `done` or `error`, the
      // server closed mid-stream. Treat as error so the UI doesn't
      // sit forever in `streaming`.
      setStatus((s) => (s === "streaming" ? "error" : s));
      setError((e) => (e ?? (status === "streaming" ? "stream ended unexpectedly" : null)));
    },
    [workspaceId, status]
  );

  return { answer, sources, status, error, search, cancel };
}
