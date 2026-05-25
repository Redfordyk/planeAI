/**
 * Multi-Agent Orchestrator UI (TZ 12.1).
 *
 * One page with three panes, all in Plane's design tokens:
 *
 *   1. Goals     — create + browse, with PLANNER preview + Apply.
 *   2. Activity  — live AgentAction feed (chronological).
 *   3. Risks     — open PredictedRisk rows with Resolve.
 *
 * Plus a kill-switch button in the header (admin-only).
 *
 * All colors via custom-* utilities (background-100/text-200/border-200/etc).
 * No hardcoded hex. Renders inside the same providers as the rest of
 * Plane so dark/light/high-contrast all come for free.
 */

"use client";

import React, { useState } from "react";
import {
  useActivityFeed,
  useApplyPlan,
  useCreateGoal,
  useGoals,
  useKillSwitch,
  useRisks,
  type Action,
  type Goal,
  type Risk,
} from "@/hooks/ai/use-orchestrator";

type Props = {
  workspaceId: string;
  workspaceSlug: string;
};

const AGENT_COLORS: Record<string, string> = {
  PLANNER: "bg-blue-500/10 text-blue-500",
  MONITOR: "bg-orange-500/10 text-orange-500",
  EXECUTOR: "bg-green-500/10 text-green-500",
  ESCALATOR: "bg-red-500/10 text-red-500",
  ANALYST: "bg-purple-500/10 text-purple-500",
  COMMUNICATOR: "bg-teal-500/10 text-teal-500",
  ORCHESTRATOR: "bg-custom-primary-100/10 text-custom-primary-100",
};

const RISK_LEVEL_BG: Record<string, string> = {
  AUTO: "bg-green-500/10 text-green-500",
  NOTIFY: "bg-yellow-500/10 text-yellow-500",
  CONFIRM: "bg-orange-500/10 text-orange-500",
  ESCALATE: "bg-red-500/10 text-red-500",
};

const IMPACT_BG: Record<string, string> = {
  low: "bg-custom-background-90 text-custom-text-300",
  medium: "bg-yellow-500/10 text-yellow-600",
  high: "bg-orange-500/10 text-orange-600",
  critical: "bg-red-500/10 text-red-500",
};

export const OrchestratorPage: React.FC<Props> = ({ workspaceId }) => {
  const { goals, loading: goalsLoading, reload: reloadGoals } = useGoals(workspaceId);
  const { actions, reload: reloadActions } = useActivityFeed(workspaceId);
  const { risks, resolve, reload: reloadRisks } = useRisks(workspaceId);
  const { engaged, flip } = useKillSwitch(workspaceId);

  return (
    <div className="flex h-full w-full flex-col gap-4 p-6 text-custom-text-100">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">🤖 ИИ-оркестратор</h1>
          <p className="text-xs text-custom-text-300">
            Цели · агенты · риски — система ведёт проект к результату.
          </p>
        </div>
        <KillSwitchButton engaged={engaged} onFlip={flip} />
      </div>

      <div className="grid flex-1 grid-cols-1 gap-4 overflow-hidden lg:grid-cols-[2fr_1fr]">
        {/* Left column: Goals */}
        <div className="flex min-h-0 flex-col gap-3 overflow-hidden">
          <GoalCreator
            workspaceId={workspaceId}
            onCreated={() => {
              reloadGoals();
              reloadActions();
            }}
          />
          <GoalList
            goals={goals}
            loading={goalsLoading}
            workspaceId={workspaceId}
            onApplied={() => {
              reloadGoals();
              reloadActions();
              reloadRisks();
            }}
          />
        </div>

        {/* Right column: Risks + Activity feed */}
        <div className="flex min-h-0 flex-col gap-3 overflow-hidden">
          <RiskPanel risks={risks} onResolve={resolve} />
          <ActivityPanel actions={actions} />
        </div>
      </div>
    </div>
  );
};

// ---- Kill switch -----------------------------------------------------

const KillSwitchButton: React.FC<{ engaged: boolean | null; onFlip: (n: boolean) => Promise<any> }> = ({
  engaged,
  onFlip,
}) => {
  if (engaged === null) return null;
  return (
    <button
      type="button"
      onClick={() => onFlip(!engaged)}
      className={`flex items-center gap-2 rounded-md border border-custom-border-200 px-3 py-1.5 text-xs font-medium transition-colors ${
        engaged
          ? "bg-red-500/10 text-red-500 hover:bg-red-500/20"
          : "bg-custom-background-90 text-custom-text-200 hover:bg-custom-background-80"
      }`}
    >
      <span>{engaged ? "🛑" : "⏸️"}</span>
      <span>{engaged ? "Агенты остановлены" : "Пауза агентов"}</span>
    </button>
  );
};

// ---- Goal creator ----------------------------------------------------

const GoalCreator: React.FC<{ workspaceId: string; onCreated: () => void }> = ({
  workspaceId,
  onCreated,
}) => {
  const { create, busy, err } = useCreateGoal(workspaceId);
  const [open, setOpen] = useState(false);
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [deadline, setDeadline] = useState("");

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    const r = await create({
      title,
      description: description || undefined,
      deadline: deadline || undefined,
      run_planner: true,
    });
    if (r.ok) {
      setTitle("");
      setDescription("");
      setDeadline("");
      setOpen(false);
      onCreated();
    }
  };

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="flex items-center justify-center gap-2 rounded-md border border-dashed border-custom-border-300 bg-custom-background-90 px-4 py-3 text-sm font-medium text-custom-text-200 hover:bg-custom-background-80 hover:text-custom-text-100"
      >
        <span>✨</span>
        <span>Поставить цель — PLANNER декомпозирует</span>
      </button>
    );
  }

  return (
    <form
      onSubmit={submit}
      className="rounded-md border border-custom-border-200 bg-custom-background-100 p-4 shadow-sm"
    >
      <input
        autoFocus
        required
        value={title}
        onChange={(e) => setTitle(e.target.value)}
        placeholder="Например: Запустить мобильное приложение к 1 июня"
        className="w-full rounded-md border border-custom-border-200 bg-custom-background-90 px-3 py-2 text-sm text-custom-text-100 placeholder:text-custom-text-400 focus:border-custom-primary-100 focus:outline-none"
      />
      <textarea
        value={description}
        onChange={(e) => setDescription(e.target.value)}
        placeholder="Описание (команда, бюджет, контекст) — опционально"
        rows={2}
        className="mt-2 w-full rounded-md border border-custom-border-200 bg-custom-background-90 px-3 py-2 text-sm text-custom-text-100 placeholder:text-custom-text-400 focus:border-custom-primary-100 focus:outline-none"
      />
      <div className="mt-2 flex items-center gap-2">
        <input
          type="date"
          value={deadline}
          onChange={(e) => setDeadline(e.target.value)}
          className="rounded-md border border-custom-border-200 bg-custom-background-90 px-3 py-2 text-sm text-custom-text-100"
        />
        <span className="text-xs text-custom-text-400">Дедлайн</span>
        <div className="flex-1" />
        <button
          type="button"
          onClick={() => setOpen(false)}
          className="rounded-md px-3 py-1.5 text-xs text-custom-text-300 hover:text-custom-text-100"
        >
          Отмена
        </button>
        <button
          type="submit"
          disabled={busy || !title.trim()}
          className="rounded-md bg-custom-primary-100 px-3 py-1.5 text-xs font-medium text-white hover:bg-custom-primary-200 disabled:opacity-50"
        >
          {busy ? "PLANNER думает..." : "Сгенерировать план"}
        </button>
      </div>
      {err && <p className="mt-2 text-xs text-red-500">{err}</p>}
    </form>
  );
};

// ---- Goal list -------------------------------------------------------

const GoalList: React.FC<{
  goals: Goal[] | null;
  loading: boolean;
  workspaceId: string;
  onApplied: () => void;
}> = ({ goals, loading, workspaceId, onApplied }) => {
  if (loading && !goals) return <Loader text="Загружаю цели..." />;
  if (!goals || goals.length === 0)
    return (
      <div className="rounded-md border border-custom-border-200 bg-custom-background-100 p-4 text-sm text-custom-text-300">
        Пока нет ни одной цели. Поставь первую — система соберёт план.
      </div>
    );
  return (
    <div className="flex flex-1 flex-col gap-2 overflow-y-auto">
      {goals.map((g) => (
        <GoalCard key={g.id} goal={g} workspaceId={workspaceId} onApplied={onApplied} />
      ))}
    </div>
  );
};

const GoalCard: React.FC<{ goal: Goal; workspaceId: string; onApplied: () => void }> = ({
  goal,
  workspaceId,
  onApplied,
}) => {
  const { apply, busy } = useApplyPlan(workspaceId);
  const [expanded, setExpanded] = useState(false);
  const plan = goal.plan_preview as any;
  const epicCount = plan?.epics?.length ?? 0;
  const taskCount = plan?.task_count ?? 0;
  const canApply = goal.status === "planning" && taskCount > 0 && goal.plan_issue_count === 0;
  const onApply = async () => {
    const r = await apply(goal.id);
    if (r.ok) onApplied();
  };
  return (
    <div className="rounded-md border border-custom-border-200 bg-custom-background-100 p-3">
      <div className="flex items-start gap-3">
        <button
          onClick={() => setExpanded((x) => !x)}
          className="flex-1 text-left"
        >
          <div className="flex items-center gap-2">
            <span className="text-sm font-semibold text-custom-text-100">{goal.title}</span>
            <StatusChip status={goal.status} />
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-3 text-xs text-custom-text-300">
            {goal.deadline && <span>📅 до {goal.deadline}</span>}
            {epicCount > 0 && <span>📋 {epicCount} эпиков · {taskCount} задач</span>}
            {goal.plan_issue_count > 0 && (
              <span className="text-green-500">✓ создано {goal.plan_issue_count} issues</span>
            )}
          </div>
        </button>
        {canApply && (
          <button
            onClick={onApply}
            disabled={busy}
            className="rounded-md bg-green-500/10 px-3 py-1.5 text-xs font-medium text-green-600 hover:bg-green-500/20 disabled:opacity-50"
          >
            {busy ? "..." : "Применить план"}
          </button>
        )}
      </div>
      {expanded && plan?.epics && (
        <div className="mt-3 space-y-2 border-t border-custom-border-200 pt-3">
          {plan.summary && (
            <p className="text-xs italic text-custom-text-300">{plan.summary}</p>
          )}
          {plan.epics.map((epic: any, i: number) => (
            <div key={i}>
              <div className="text-xs font-semibold text-custom-text-200">{epic.name}</div>
              <ul className="ml-3 list-disc text-xs text-custom-text-300">
                {(epic.tasks ?? []).slice(0, 8).map((t: any, j: number) => (
                  <li key={j}>
                    <span className="text-custom-text-200">{t.name}</span>
                    {t.estimated_hours && (
                      <span className="ml-1 text-custom-text-400">({t.estimated_hours}ч)</span>
                    )}
                  </li>
                ))}
                {epic.tasks?.length > 8 && (
                  <li className="text-custom-text-400">… ещё {epic.tasks.length - 8}</li>
                )}
              </ul>
            </div>
          ))}
        </div>
      )}
    </div>
  );
};

const StatusChip: React.FC<{ status: string }> = ({ status }) => {
  const cls: Record<string, string> = {
    draft: "bg-custom-background-90 text-custom-text-300",
    planning: "bg-blue-500/10 text-blue-500",
    executing: "bg-green-500/10 text-green-500",
    at_risk: "bg-yellow-500/10 text-yellow-600",
    blocked: "bg-red-500/10 text-red-500",
    done: "bg-custom-background-80 text-custom-text-300",
  };
  return (
    <span className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${cls[status] ?? cls.draft}`}>
      {status}
    </span>
  );
};

// ---- Risks ----------------------------------------------------------

const RiskPanel: React.FC<{ risks: Risk[]; onResolve: (id: string) => Promise<any> }> = ({
  risks,
  onResolve,
}) => (
  <div className="rounded-md border border-custom-border-200 bg-custom-background-100 p-3">
    <div className="mb-2 flex items-center justify-between">
      <h3 className="text-sm font-semibold text-custom-text-100">🚨 Открытые риски</h3>
      <span className="text-xs text-custom-text-400">{risks.length}</span>
    </div>
    {risks.length === 0 ? (
      <p className="text-xs text-custom-text-300">MONITOR не видит проблем.</p>
    ) : (
      <ul className="space-y-2">
        {risks.slice(0, 6).map((r) => (
          <li
            key={r.id}
            className="rounded border border-custom-border-200 bg-custom-background-90 p-2"
          >
            <div className="flex items-start justify-between gap-2">
              <div className="flex-1">
                <div className="flex items-center gap-1.5">
                  <span
                    className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${
                      IMPACT_BG[r.impact] ?? IMPACT_BG.medium
                    }`}
                  >
                    {r.impact}
                  </span>
                  <span className="text-xs text-custom-text-200">{r.risk_type}</span>
                  <span className="text-[10px] text-custom-text-400">
                    {Math.round(r.confidence * 100)}%
                  </span>
                </div>
                <p className="mt-1 text-xs text-custom-text-300">{r.rationale}</p>
              </div>
              <button
                onClick={() => onResolve(r.id)}
                className="text-xs text-custom-text-400 hover:text-custom-text-100"
                title="Отметить как решённый"
              >
                ✓
              </button>
            </div>
          </li>
        ))}
      </ul>
    )}
  </div>
);

// ---- Activity feed --------------------------------------------------

const ActivityPanel: React.FC<{ actions: Action[] }> = ({ actions }) => (
  <div className="flex min-h-0 flex-1 flex-col rounded-md border border-custom-border-200 bg-custom-background-100 p-3">
    <h3 className="mb-2 text-sm font-semibold text-custom-text-100">🧠 Лента агентов</h3>
    {actions.length === 0 ? (
      <p className="text-xs text-custom-text-300">Пока тихо.</p>
    ) : (
      <ul className="flex-1 space-y-1.5 overflow-y-auto">
        {actions.map((a) => (
          <li key={a.id} className="text-xs">
            <div className="flex items-center gap-1.5">
              <span
                className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${
                  AGENT_COLORS[a.agent_type] ?? "bg-custom-background-90 text-custom-text-300"
                }`}
              >
                {a.agent_type}
              </span>
              <span className="text-custom-text-200">{a.action_type}</span>
              <span
                className={`rounded px-1 py-0.5 text-[9px] ${
                  RISK_LEVEL_BG[a.risk_level] ?? "bg-custom-background-90 text-custom-text-300"
                }`}
              >
                {a.risk_level}
              </span>
              <span className="ml-auto text-[10px] text-custom-text-400">
                {new Date(a.created_at).toLocaleTimeString("ru-RU", {
                  hour: "2-digit",
                  minute: "2-digit",
                })}
              </span>
            </div>
            {a.reasoning && (
              <p className="ml-1 mt-0.5 truncate text-[11px] text-custom-text-300">
                {a.reasoning}
              </p>
            )}
          </li>
        ))}
      </ul>
    )}
  </div>
);

const Loader: React.FC<{ text: string }> = ({ text }) => (
  <div className="flex items-center justify-center gap-2 p-4 text-xs text-custom-text-300">
    <span className="h-3 w-3 animate-spin rounded-full border-2 border-custom-primary-100 border-t-transparent" />
    {text}
  </div>
);

export default OrchestratorPage;
