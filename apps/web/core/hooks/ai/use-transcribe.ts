/**
 * Hook for the Whisper transcribe endpoint
 * (POST /api/ai/workspaces/<id>/transcribe/, multipart).
 */

import { useCallback, useState } from "react";

import { csrfHeaders } from "./csrf";

export type UseTranscribeResult = {
  loading: boolean;
  error: string | null;
  transcribe: (audio: Blob, language?: string) => Promise<string | null>;
};

export function useTranscribe(workspaceId: string): UseTranscribeResult {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const transcribe = useCallback(
    async (audio: Blob, language: string = "ru"): Promise<string | null> => {
      setLoading(true);
      setError(null);
      try {
        const fd = new FormData();
        const ext = audio.type.includes("mp4") ? "m4a" : "webm";
        fd.append("audio", audio, `clip.${ext}`);
        fd.append("language", language);
        // DON'T set Content-Type — let the browser add the
        // multipart boundary. csrfHeaders() only adds X-CSRFToken.
        const headers = await csrfHeaders();
        const resp = await fetch(
          `/api/ai/workspaces/${workspaceId}/transcribe/`,
          { method: "POST", body: fd, headers, credentials: "same-origin" }
        );
        if (!resp.ok) {
          const body = await resp.json().catch(() => ({}));
          setError(body.error || `HTTP ${resp.status}`);
          return null;
        }
        const json = (await resp.json()) as { text?: string };
        return (json.text || "").trim();
      } catch (e) {
        setError((e as Error).message || "network error");
        return null;
      } finally {
        setLoading(false);
      }
    },
    [workspaceId]
  );

  return { loading, error, transcribe };
}
