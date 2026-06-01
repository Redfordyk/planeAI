/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Bot, CheckCircle2, GitBranch, Loader2, PlayCircle, RefreshCw, Rocket, ShieldCheck, FileText, XCircle } from "lucide-react";
// plane imports
import { useTranslation } from "@plane/i18n";
import { cn } from "@plane/utils";
// hooks
import { useWorkspace } from "@/hooks/store/use-workspace";
import {
  type AngelaDeployMode,
  type AngelaRun,
  type AngelaTargets,
  isTerminal,
  useAngela,
} from "@/hooks/ai/use-angela";

const PHASE_LABELS: Record<string, string> = {
  plan: "Plan",
  code: "Code",
  review: "Review",
  test: "Test",
  deploy: "Deploy",
  docs: "Docs",
};

const STATUS_COLOR: Record<AngelaRun["status"], string> = {
  queued: "text-custom-text-300",
  coding: "text-blue-500",
  reviewing: "text-blue-500",
  testing: "text-amber-500",
  deploying: "text-purple-500",
  awaiting_approval: "text-orange-500",
  succeeded: "text-green-600",
  failed: "text-red-600",
  cancelled: "text-custom-text-400",
};

type DeployModeButton = {
  mode: AngelaDeployMode;
  icon: React.ReactNode;
  title: string;
  subtitle: string;
  accent: string;
};

export function AngelaConsole() {
  const { t } = useTranslation();
  const { currentWorkspace } = useWorkspace();
  const workspaceId = currentWorkspace?.id;
  const api = useAngela(workspaceId);

  const [prompt, setPrompt] = useState("");
  const [target, setTarget] = useState<string>("");
  const [targets, setTargets] = useState<AngelaTargets | null>(null);
  const [runs, setRuns] = useState<AngelaRun[]>([]);
  const [selected, setSelected] = useState<AngelaRun | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // --- bootstrap -------------------------------------------------------
  useEffect(() => {
    if (!workspaceId) return;
    api.targets().then((tg) => {
      setTargets(tg);
      setTarget((cur) => cur || tg.default_target);
    }).catch((e) => setError(String(e)));
    api.listRuns().then(setRuns).catch((e) => setError(String(e)));
  }, [workspaceId]); // eslint-disable-line react-hooks/exhaustive-deps

  // --- poll the selected run while it's live ---------------------------
  const stopPoll = useCallback(() => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  const openRun = useCallback(
    async (runId: string) => {
      stopPoll();
      try {
        const r = await api.getRun(runId);
        setSelected(r);
        if (!isTerminal(r.status)) {
          pollRef.current = setInterval(async () => {
            try {
              const fresh = await api.getRun(runId);
              setSelected(fresh);
              setRuns((prev) => prev.map((x) => (x.id === fresh.id ? { ...x, status: fresh.status } : x)));
              if (isTerminal(fresh.status)) stopPoll();
            } catch {
              /* keep polling */
            }
          }, 2500);
        }
      } catch (e) {
        setError(String(e));
      }
    },
    [api, stopPoll]
  );

  useEffect(() => () => stopPoll(), [stopPoll]);

  // --- actions ---------------------------------------------------------
  const launch = useCallback(
    async (mode: AngelaDeployMode) => {
      if (!prompt.trim()) {
        setError(t("angela.error_prompt_required"));
        return;
      }
      setBusy(true);
      setError(null);
      try {
        const run = await api.startRun({ prompt: prompt.trim(), target, deployMode: mode });
        setRuns((prev) => [run, ...prev]);
        await openRun(run.id);
      } catch (e) {
        setError(String(e));
      } finally {
        setBusy(false);
      }
    },
    [api, prompt, target, openRun, t]
  );

  const doApprove = useCallback(async () => {
    if (!selected) return;
    setBusy(true);
    try {
      await api.approveProd(selected.id);
      await openRun(selected.id);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }, [api, selected, openRun]);

  const doManualDeploy = useCallback(async () => {
    if (!selected) return;
    setBusy(true);
    try {
      await api.manualDeploy(selected.id);
      await openRun(selected.id);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }, [api, selected, openRun]);

  const doDocs = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      const run = await api.generateDocs({ target });
      setRuns((prev) => [run, ...prev]);
      await openRun(run.id);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }, [api, target, openRun]);

  const modeButtons: DeployModeButton[] = [
    {
      mode: "staging_gate",
      icon: <ShieldCheck className="size-4" />,
      title: t("angela.mode_staging_gate_title"),
      subtitle: t("angela.mode_staging_gate_sub"),
      accent: "border-orange-400/60 hover:bg-orange-500/10 text-orange-600",
    },
    {
      mode: "autonomous_prod",
      icon: <Rocket className="size-4" />,
      title: t("angela.mode_autonomous_title"),
      subtitle: t("angela.mode_autonomous_sub"),
      accent: "border-red-400/60 hover:bg-red-500/10 text-red-600",
    },
    {
      mode: "manual",
      icon: <GitBranch className="size-4" />,
      title: t("angela.mode_manual_title"),
      subtitle: t("angela.mode_manual_sub"),
      accent: "border-blue-400/60 hover:bg-blue-500/10 text-blue-600",
    },
  ];

  if (!workspaceId) return null;

  return (
    <div className="flex h-full flex-col gap-4 p-6">
      {/* header */}
      <div className="flex items-center gap-2">
        <Bot className="size-6 text-custom-primary-100" />
        <h1 className="text-xl font-semibold">{t("angela.title")}</h1>
        <span className="ml-2 rounded bg-custom-background-80 px-2 py-0.5 text-11 text-custom-text-300">
          {t("angela.sandbox_badge")}
        </span>
      </div>

      <div className="grid min-h-0 flex-1 grid-cols-1 gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.2fr)]">
        {/* left: compose + runs */}
        <div className="flex min-h-0 flex-col gap-4 overflow-y-auto">
          <div className="rounded-lg border border-custom-border-200 p-4">
            <label className="mb-1 block text-13 font-medium">{t("angela.task_label")}</label>
            <textarea
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              placeholder={t("angela.task_placeholder")}
              rows={4}
              className="w-full resize-y rounded-md border border-custom-border-200 bg-custom-background-100 p-2 text-13 outline-none focus:border-custom-primary-100"
            />

            <div className="mt-3 flex items-center gap-2">
              <label className="text-12 text-custom-text-300">{t("angela.target_label")}</label>
              <select
                value={target}
                onChange={(e) => setTarget(e.target.value)}
                className="rounded-md border border-custom-border-200 bg-custom-background-100 px-2 py-1 text-12"
              >
                {(targets?.targets ?? []).map((tk) => (
                  <option key={tk} value={tk}>
                    {tk}
                  </option>
                ))}
              </select>
            </div>

            <div className="mt-3 grid grid-cols-1 gap-2">
              {modeButtons.map((b) => (
                <button
                  key={b.mode}
                  disabled={busy}
                  onClick={() => launch(b.mode)}
                  className={cn(
                    "flex items-center gap-3 rounded-md border px-3 py-2 text-left transition disabled:opacity-50",
                    b.accent
                  )}
                >
                  {b.icon}
                  <span className="flex flex-col">
                    <span className="text-13 font-medium">{b.title}</span>
                    <span className="text-11 text-custom-text-300">{b.subtitle}</span>
                  </span>
                  <PlayCircle className="ml-auto size-4 opacity-70" />
                </button>
              ))}
            </div>

            <button
              disabled={busy}
              onClick={doDocs}
              className="mt-2 flex w-full items-center justify-center gap-2 rounded-md border border-custom-border-200 px-3 py-2 text-13 hover:bg-custom-background-80 disabled:opacity-50"
            >
              <FileText className="size-4" />
              {t("angela.generate_docs")}
            </button>

            {error && <p className="mt-2 text-12 text-red-600">{error}</p>}
          </div>

          {/* runs list */}
          <div className="rounded-lg border border-custom-border-200">
            <div className="flex items-center justify-between border-b border-custom-border-200 px-3 py-2">
              <span className="text-13 font-medium">{t("angela.runs_title")}</span>
              <button
                onClick={() => api.listRuns().then(setRuns).catch((e) => setError(String(e)))}
                className="text-custom-text-300 hover:text-custom-text-100"
                title={t("angela.refresh")}
              >
                <RefreshCw className="size-3.5" />
              </button>
            </div>
            <ul className="max-h-[40vh] divide-y divide-custom-border-200 overflow-y-auto">
              {runs.length === 0 && (
                <li className="px-3 py-4 text-12 text-custom-text-300">{t("angela.no_runs")}</li>
              )}
              {runs.map((r) => (
                <li key={r.id}>
                  <button
                    onClick={() => openRun(r.id)}
                    className={cn(
                      "flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-custom-background-80",
                      selected?.id === r.id && "bg-custom-background-80"
                    )}
                  >
                    <RunStatusIcon status={r.status} />
                    <span className="flex min-w-0 flex-col">
                      <span className="truncate text-12 font-medium">{r.prompt || r.target_repo}</span>
                      <span className="text-11 text-custom-text-300">
                        {r.deploy_mode} · {r.target_repo}
                      </span>
                    </span>
                    <span className={cn("ml-auto text-11", STATUS_COLOR[r.status])}>{r.status}</span>
                  </button>
                </li>
              ))}
            </ul>
          </div>
        </div>

        {/* right: selected run detail + feed */}
        <div className="flex min-h-0 flex-col overflow-hidden rounded-lg border border-custom-border-200">
          {!selected ? (
            <div className="flex flex-1 items-center justify-center text-13 text-custom-text-300">
              {t("angela.select_run")}
            </div>
          ) : (
            <>
              <div className="border-b border-custom-border-200 px-4 py-3">
                <div className="flex items-center gap-2">
                  <RunStatusIcon status={selected.status} />
                  <span className={cn("text-13 font-semibold", STATUS_COLOR[selected.status])}>
                    {selected.status}
                  </span>
                  {selected.branch && (
                    <span className="flex items-center gap-1 text-11 text-custom-text-300">
                      <GitBranch className="size-3" />
                      {selected.branch}
                    </span>
                  )}
                  <span className="ml-auto text-11 text-custom-text-300">
                    {t("angela.iterations")}: {selected.iterations}
                  </span>
                </div>
                <p className="mt-1 truncate text-12 text-custom-text-200">{selected.prompt}</p>

                {/* contextual action row */}
                <div className="mt-2 flex flex-wrap items-center gap-2">
                  {selected.status === "awaiting_approval" && (
                    <button
                      disabled={busy}
                      onClick={doApprove}
                      className="flex items-center gap-1.5 rounded-md bg-orange-500 px-3 py-1.5 text-12 font-medium text-white hover:bg-orange-600 disabled:opacity-50"
                    >
                      <ShieldCheck className="size-3.5" />
                      {t("angela.approve_prod")}
                    </button>
                  )}
                  {selected.deploy_mode === "manual" &&
                    selected.test_passed === true &&
                    selected.status === "succeeded" && (
                      <button
                        disabled={busy}
                        onClick={doManualDeploy}
                        className="flex items-center gap-1.5 rounded-md bg-blue-600 px-3 py-1.5 text-12 font-medium text-white hover:bg-blue-700 disabled:opacity-50"
                      >
                        <Rocket className="size-3.5" />
                        {t("angela.deploy_now")}
                      </button>
                    )}
                  {selected.deploy_url && (
                    <a
                      href={selected.deploy_url}
                      target="_blank"
                      rel="noreferrer"
                      className="text-12 text-custom-primary-100 underline"
                    >
                      {t("angela.open_deploy")} ↗
                    </a>
                  )}
                  {selected.wiki_url && (
                    <a
                      href={selected.wiki_url}
                      target="_blank"
                      rel="noreferrer"
                      className="text-12 text-custom-primary-100 underline"
                    >
                      {t("angela.open_wiki")} ↗
                    </a>
                  )}
                </div>
                {selected.error && <p className="mt-2 text-11 text-red-600">{selected.error}</p>}
              </div>

              {/* step feed */}
              <ol className="flex-1 space-y-2 overflow-y-auto p-4">
                {(selected.steps ?? []).map((s) => (
                  <li key={s.id} className="flex gap-2">
                    <StepIcon status={s.status} />
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="rounded bg-custom-background-80 px-1.5 py-0.5 text-10 uppercase text-custom-text-300">
                          {PHASE_LABELS[s.phase] ?? s.phase}
                        </span>
                        <span className="text-12 font-medium">{s.title}</span>
                      </div>
                      {s.detail && (
                        <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap rounded bg-custom-background-90 p-2 text-11 text-custom-text-300">
                          {s.detail}
                        </pre>
                      )}
                    </div>
                  </li>
                ))}
                {(selected.steps ?? []).length === 0 && (
                  <li className="text-12 text-custom-text-300">{t("angela.no_steps")}</li>
                )}
              </ol>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function RunStatusIcon({ status }: { status: AngelaRun["status"] }) {
  if (status === "succeeded") return <CheckCircle2 className="size-4 flex-shrink-0 text-green-600" />;
  if (status === "failed") return <XCircle className="size-4 flex-shrink-0 text-red-600" />;
  if (status === "cancelled") return <XCircle className="size-4 flex-shrink-0 text-custom-text-400" />;
  if (status === "awaiting_approval") return <ShieldCheck className="size-4 flex-shrink-0 text-orange-500" />;
  return <Loader2 className="size-4 flex-shrink-0 animate-spin text-blue-500" />;
}

function StepIcon({ status }: { status: AngelaStepStatus }) {
  if (status === "ok") return <CheckCircle2 className="mt-0.5 size-3.5 flex-shrink-0 text-green-600" />;
  if (status === "failed") return <XCircle className="mt-0.5 size-3.5 flex-shrink-0 text-red-600" />;
  if (status === "skipped") return <GitBranch className="mt-0.5 size-3.5 flex-shrink-0 text-custom-text-400" />;
  return <Loader2 className="mt-0.5 size-3.5 flex-shrink-0 animate-spin text-blue-500" />;
}

type AngelaStepStatus = "started" | "ok" | "failed" | "skipped";
