/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  Bot,
  CheckCircle2,
  FileText,
  GitBranch,
  Loader2,
  PlayCircle,
  RefreshCw,
  Rocket,
  ShieldCheck,
  XCircle,
} from "lucide-react";
// plane imports
import { useTranslation } from "@plane/i18n";
import { Button } from "@plane/propel/button";
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
  queued: "text-tertiary",
  coding: "text-accent-primary",
  reviewing: "text-accent-primary",
  testing: "text-warning-primary",
  deploying: "text-accent-primary",
  awaiting_approval: "text-warning-primary",
  succeeded: "text-success-primary",
  failed: "text-danger-primary",
  cancelled: "text-tertiary",
};

type DeployModeButton = {
  mode: AngelaDeployMode;
  icon: React.ReactNode;
  title: string;
  subtitle: string;
  chip: string;
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
    api
      .targets()
      .then((tg) => {
        setTargets(tg);
        setTarget((cur) => cur || tg.default_target);
      })
      .catch((e) => setError(String(e)));
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

  const refreshRuns = useCallback(() => {
    api.listRuns().then(setRuns).catch((e) => setError(String(e)));
  }, [api]);

  const modeButtons: DeployModeButton[] = [
    {
      mode: "staging_gate",
      icon: <ShieldCheck className="size-4" />,
      title: t("angela.mode_staging_gate_title"),
      subtitle: t("angela.mode_staging_gate_sub"),
      chip: "bg-warning-subtle text-warning-primary",
    },
    {
      mode: "autonomous_prod",
      icon: <Rocket className="size-4" />,
      title: t("angela.mode_autonomous_title"),
      subtitle: t("angela.mode_autonomous_sub"),
      chip: "bg-danger-subtle text-danger-primary",
    },
    {
      mode: "manual",
      icon: <GitBranch className="size-4" />,
      title: t("angela.mode_manual_title"),
      subtitle: t("angela.mode_manual_sub"),
      chip: "bg-accent-subtle text-accent-primary",
    },
  ];

  if (!workspaceId) return null;

  return (
    <div className="flex h-full flex-col gap-4 p-6">
      {/* header */}
      <div className="flex items-center gap-2">
        <span className="flex size-7 items-center justify-center rounded-md bg-accent-subtle text-accent-primary">
          <Bot className="size-4" />
        </span>
        <h3 className="text-lg font-semibold text-primary">{t("angela.title")}</h3>
        <span className="ml-1 rounded bg-layer-3 px-1.5 py-0.5 text-11 font-medium text-tertiary">
          {t("angela.sandbox_badge")}
        </span>
      </div>

      <div className="grid min-h-0 flex-1 grid-cols-1 gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.2fr)]">
        {/* left: compose + runs */}
        <div className="flex min-h-0 flex-col gap-4 overflow-y-auto">
          <div className="rounded-md border border-subtle-1 bg-layer-1 p-4">
            <label className="mb-1.5 block text-13 font-medium text-secondary">{t("angela.task_label")}</label>
            <textarea
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              placeholder={t("angela.task_placeholder")}
              rows={4}
              className="w-full resize-y rounded-md border border-subtle-1 bg-layer-1 p-2.5 text-13 text-primary placeholder:text-placeholder outline-none transition-colors focus:border-accent-primary"
            />

            <div className="mt-3 flex items-center gap-2">
              <label className="text-12 text-tertiary">{t("angela.target_label")}</label>
              <select
                value={target}
                onChange={(e) => setTarget(e.target.value)}
                className="rounded-md border border-subtle-1 bg-layer-1 px-2 py-1 text-12 text-primary outline-none focus:border-accent-primary"
              >
                {(targets?.targets ?? []).map((tk) => (
                  <option key={tk} value={tk}>
                    {tk}
                  </option>
                ))}
              </select>
            </div>

            <div className="mt-4 flex flex-col gap-2">
              <span className="text-11 font-medium uppercase tracking-wide text-tertiary">
                {t("angela.deploy_mode_label")}
              </span>
              {modeButtons.map((b) => (
                <button
                  key={b.mode}
                  disabled={busy}
                  onClick={() => launch(b.mode)}
                  className={cn(
                    "group flex items-center gap-3 rounded-md border border-subtle-1 bg-layer-1 px-3 py-2.5 text-left transition-colors",
                    "hover:bg-layer-2 disabled:pointer-events-none disabled:opacity-50"
                  )}
                >
                  <span className={cn("flex size-7 flex-shrink-0 items-center justify-center rounded-md", b.chip)}>
                    {b.icon}
                  </span>
                  <span className="flex min-w-0 flex-col">
                    <span className="text-13 font-medium text-primary">{b.title}</span>
                    <span className="text-11 text-tertiary">{b.subtitle}</span>
                  </span>
                  <PlayCircle className="ml-auto size-4 flex-shrink-0 text-tertiary transition-colors group-hover:text-accent-primary" />
                </button>
              ))}
            </div>

            <Button
              variant="secondary"
              size="lg"
              className="mt-3 w-full"
              disabled={busy}
              onClick={doDocs}
              prependIcon={<FileText />}
            >
              {t("angela.generate_docs")}
            </Button>

            {error && <p className="mt-2 text-12 text-danger-primary">{error}</p>}
          </div>

          {/* runs list */}
          <div className="rounded-md border border-subtle-1 bg-layer-1">
            <div className="flex items-center justify-between border-b border-subtle-1 px-3 py-2">
              <span className="text-13 font-medium text-primary">{t("angela.runs_title")}</span>
              <button
                onClick={refreshRuns}
                className="text-tertiary transition-colors hover:text-primary"
                title={t("angela.refresh")}
              >
                <RefreshCw className="size-3.5" />
              </button>
            </div>
            <ul className="max-h-[40vh] divide-y divide-subtle-1 overflow-y-auto">
              {runs.length === 0 && <li className="px-3 py-4 text-12 text-tertiary">{t("angela.no_runs")}</li>}
              {runs.map((r) => (
                <li key={r.id}>
                  <button
                    onClick={() => openRun(r.id)}
                    className={cn(
                      "flex w-full items-center gap-2 px-3 py-2 text-left transition-colors hover:bg-layer-2",
                      selected?.id === r.id && "bg-layer-2"
                    )}
                  >
                    <RunStatusIcon status={r.status} />
                    <span className="flex min-w-0 flex-col">
                      <span className="truncate text-12 font-medium text-primary">{r.prompt || r.target_repo}</span>
                      <span className="text-11 text-tertiary">
                        {r.deploy_mode} · {r.target_repo}
                      </span>
                    </span>
                    <span className={cn("ml-auto text-11 font-medium", STATUS_COLOR[r.status])}>{r.status}</span>
                  </button>
                </li>
              ))}
            </ul>
          </div>
        </div>

        {/* right: selected run detail + feed */}
        <div className="flex min-h-0 flex-col overflow-hidden rounded-md border border-subtle-1 bg-layer-1">
          {!selected ? (
            <div className="flex flex-1 items-center justify-center text-13 text-tertiary">
              {t("angela.select_run")}
            </div>
          ) : (
            <>
              <div className="border-b border-subtle-1 px-4 py-3">
                <div className="flex items-center gap-2">
                  <RunStatusIcon status={selected.status} />
                  <span className={cn("text-13 font-semibold", STATUS_COLOR[selected.status])}>{selected.status}</span>
                  {selected.branch && (
                    <span className="flex items-center gap-1 text-11 text-tertiary">
                      <GitBranch className="size-3" />
                      {selected.branch}
                    </span>
                  )}
                  <span className="ml-auto text-11 text-tertiary">
                    {t("angela.iterations")}: {selected.iterations}
                  </span>
                </div>
                <p className="mt-1 truncate text-12 text-secondary">{selected.prompt}</p>

                {/* contextual action row */}
                <div className="mt-2.5 flex flex-wrap items-center gap-2">
                  {selected.status === "awaiting_approval" && (
                    <Button variant="primary" size="sm" disabled={busy} onClick={doApprove} prependIcon={<ShieldCheck />}>
                      {t("angela.approve_prod")}
                    </Button>
                  )}
                  {selected.deploy_mode === "manual" &&
                    selected.test_passed === true &&
                    selected.status === "succeeded" && (
                      <Button variant="primary" size="sm" disabled={busy} onClick={doManualDeploy} prependIcon={<Rocket />}>
                        {t("angela.deploy_now")}
                      </Button>
                    )}
                  {selected.deploy_url && (
                    <a
                      href={selected.deploy_url}
                      target="_blank"
                      rel="noreferrer"
                      className="text-12 font-medium text-link-primary hover:text-link-primary-hover"
                    >
                      {t("angela.open_deploy")} ↗
                    </a>
                  )}
                  {selected.wiki_url && (
                    <a
                      href={selected.wiki_url}
                      target="_blank"
                      rel="noreferrer"
                      className="text-12 font-medium text-link-primary hover:text-link-primary-hover"
                    >
                      {t("angela.open_wiki")} ↗
                    </a>
                  )}
                </div>
                {selected.error && <p className="mt-2 text-11 text-danger-primary">{selected.error}</p>}
              </div>

              {/* step feed */}
              <ol className="flex-1 space-y-3 overflow-y-auto p-4">
                {(selected.steps ?? []).map((s) => (
                  <li key={s.id} className="flex gap-2">
                    <StepIcon status={s.status} />
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <span className="rounded bg-layer-3 px-1.5 py-0.5 text-10 font-medium uppercase tracking-wide text-tertiary">
                          {PHASE_LABELS[s.phase] ?? s.phase}
                        </span>
                        <span className="text-12 font-medium text-primary">{s.title}</span>
                      </div>
                      {s.detail && (
                        <pre className="mt-1.5 max-h-40 overflow-auto whitespace-pre-wrap rounded-md bg-layer-2 p-2.5 text-11 text-secondary">
                          {s.detail}
                        </pre>
                      )}
                    </div>
                  </li>
                ))}
                {(selected.steps ?? []).length === 0 && (
                  <li className="text-12 text-tertiary">{t("angela.no_steps")}</li>
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
  if (status === "succeeded") return <CheckCircle2 className="size-4 flex-shrink-0 text-success-primary" />;
  if (status === "failed") return <XCircle className="size-4 flex-shrink-0 text-danger-primary" />;
  if (status === "cancelled") return <XCircle className="size-4 flex-shrink-0 text-tertiary" />;
  if (status === "awaiting_approval") return <ShieldCheck className="size-4 flex-shrink-0 text-warning-primary" />;
  return <Loader2 className="size-4 flex-shrink-0 animate-spin text-accent-primary" />;
}

function StepIcon({ status }: { status: AngelaStepStatus }) {
  if (status === "ok") return <CheckCircle2 className="mt-0.5 size-3.5 flex-shrink-0 text-success-primary" />;
  if (status === "failed") return <XCircle className="mt-0.5 size-3.5 flex-shrink-0 text-danger-primary" />;
  if (status === "skipped") return <GitBranch className="mt-0.5 size-3.5 flex-shrink-0 text-tertiary" />;
  return <Loader2 className="mt-0.5 size-3.5 flex-shrink-0 animate-spin text-accent-primary" />;
}

type AngelaStepStatus = "started" | "ok" | "failed" | "skipped";
