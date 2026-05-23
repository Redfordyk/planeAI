/**
 * Minimal microphone-record hook built on the browser's MediaRecorder
 * API. No external deps — works in any modern browser served over
 * HTTPS (the API rejects insecure contexts).
 *
 * State machine: idle -> requesting -> recording -> processing -> idle.
 * Returns a Blob (webm/opus) on stop; the caller uploads it to the
 * /transcribe/ endpoint. Errors surface as `error` (string).
 */

import { useCallback, useEffect, useRef, useState } from "react";

export type VoiceState = "idle" | "requesting" | "recording" | "processing";

export type UseVoiceRecorderResult = {
  state: VoiceState;
  error: string | null;
  /** ms of recorded audio, ticked from a setInterval while recording */
  durationMs: number;
  start: () => Promise<void>;
  /** Stops recording and resolves to the captured audio blob */
  stop: () => Promise<Blob | null>;
};

export function useVoiceRecorder(): UseVoiceRecorderResult {
  const [state, setState] = useState<VoiceState>("idle");
  const [error, setError] = useState<string | null>(null);
  const [durationMs, setDurationMs] = useState(0);

  const recorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<BlobPart[]>([]);
  const streamRef = useRef<MediaStream | null>(null);
  const intervalRef = useRef<number | null>(null);
  const startedAtRef = useRef<number>(0);
  const resolveStopRef = useRef<((b: Blob | null) => void) | null>(null);

  // Tear down on unmount
  useEffect(
    () => () => {
      streamRef.current?.getTracks().forEach((t) => t.stop());
      if (intervalRef.current) window.clearInterval(intervalRef.current);
    },
    []
  );

  const start = useCallback(async () => {
    setError(null);
    if (state !== "idle") return;
    if (!navigator.mediaDevices?.getUserMedia) {
      setError("Этот браузер не поддерживает запись звука");
      return;
    }
    setState("requesting");
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true },
      });
      streamRef.current = stream;

      // webm/opus is the broadest support on Chrome/Firefox/Edge;
      // Safari uses mp4. Whisper accepts both.
      let mimeType = "audio/webm;codecs=opus";
      if (!MediaRecorder.isTypeSupported(mimeType)) {
        mimeType = MediaRecorder.isTypeSupported("audio/mp4") ? "audio/mp4" : "";
      }
      const rec = mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream);
      recorderRef.current = rec;
      chunksRef.current = [];

      rec.ondataavailable = (e) => {
        if (e.data && e.data.size > 0) chunksRef.current.push(e.data);
      };
      rec.onstop = () => {
        const type = rec.mimeType || "audio/webm";
        const blob = new Blob(chunksRef.current, { type });
        streamRef.current?.getTracks().forEach((t) => t.stop());
        streamRef.current = null;
        if (intervalRef.current) window.clearInterval(intervalRef.current);
        intervalRef.current = null;
        recorderRef.current = null;
        chunksRef.current = [];
        if (resolveStopRef.current) {
          resolveStopRef.current(blob.size > 0 ? blob : null);
          resolveStopRef.current = null;
        }
      };

      rec.start(250); // emit chunks every 250 ms
      startedAtRef.current = Date.now();
      setDurationMs(0);
      intervalRef.current = window.setInterval(() => {
        setDurationMs(Date.now() - startedAtRef.current);
      }, 200);
      setState("recording");
    } catch (e) {
      setError(`Доступ к микрофону: ${(e as Error).message || "отказано"}`);
      streamRef.current?.getTracks().forEach((t) => t.stop());
      streamRef.current = null;
      setState("idle");
    }
  }, [state]);

  const stop = useCallback(async (): Promise<Blob | null> => {
    if (state !== "recording") return null;
    setState("processing");
    return new Promise<Blob | null>((resolve) => {
      resolveStopRef.current = resolve;
      try {
        recorderRef.current?.stop();
      } catch {
        resolve(null);
        setState("idle");
      }
    }).finally(() => {
      setState("idle");
    });
  }, [state]);

  return { state, error, durationMs, start, stop };
}
