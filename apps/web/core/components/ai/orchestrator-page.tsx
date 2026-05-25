/**
 * Multi-Agent Orchestrator UI (TZ 12.1) — native Plane styling.
 *
 * All visuals use Plane's design system:
 *   - components: @plane/propel Card / Button / Badge / Input
 *   - semantic tokens: bg-layer-1/2/3, text-primary/secondary/tertiary,
 *     border-strong/subtle, accent-*, danger-*, warning-*, success-*
 *   - icons: lucide-react (the same as everywhere in Plane)
 *
 * No hex literals, no bg-custom-* utility classes — only semantic
 * tokens so light/dark/high-contrast all work for free.
 */

"use client";

import React, { useState } from "react";
import {
  Activity,
  AlertTriangle,
  Calendar,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Clock,
  ListChecks,
  Pause,
  PlayCircle,
  Plus,
  ShieldAlert,
  Sparkles,
  Target,
  X,
} from "lucide-react";
import { Badge } from "@plane/propel/badge";
import { Button } from "@plane/propel/button";
import { Card, ECardSpacing, ECardVariant } from "@plane/propel/card";
import { Input } from "@plane/propel/input";
import { Spinner } from "@plane/propel/spinners";
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

const AGENT_VARIANT: Record<string, "neutral" | "brand" | "warning" | "success" | "danger"> = {
  PLANNER: "brand",
  MONITOR: "warning",
  EXECUTOR: "success",
  ESCALATOR: "danger",
  ANALYST: "brand",
  COMMUNICATOR: "neutral",
  ORCHESTRATOR: "brand",
};

const RISK_BADGE: Record<string, "neutral" | "brand" | "warning" | "success" | "danger"> = {
  AUTO: "success",
  NOTIFY: "warning",
  CONFIRM: "warning",
  ESCALATE: "danger",
};

const IMPACT_BADGE: Record<string, "neutral" | "warning" | "danger"> = {
  low: "neutral",
  medium: "neutral",
  high: "warning",
  critical: "danger",
};

const STATUS_BADGE: Record<string, "neutral" | "brand" | "warning" | "success" | "danger"> = {
  draft: "neutral",
  planning: "brand",
  executing: "success",
  at_risk: "warning",
  blocked: "danger",
  done: "neutral",
};

export const OrchestratorPage: React.FC<Props> = ({ workspaceId }) => {
  const { goals, loading: goalsLoading, reload: reloadGoals } = useGoals(workspaceId);
  const { actions, reload: reloadActions } = useActivityFeed(workspaceId);
  const { risks, resolve, reload: reloadRisks } = useRisks(workspaceId);
  const { engaged, flip } = useKillSwitch(workspaceId);

  return (
    <div className="flex h-full w-full flex-col gap-5 bg-layer-1 p-6 text-primary">
      <PageHeader engaged={engaged} onFlip={flip} />

      <div className="grid flex-1 grid-cols-1 gap-5 overflow-hidden lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
        {/* Left column — Goals */}
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

        {/* Right column — Risks + Activity */}
        <div className="flex min-h-0 flex-col gap-3 overflow-hidden">
          <RiskPanel risks={risks} onResolve={resolve} />
          <ActivityPanel actions={actions} />
        </div>
      </div>
    </div>
  );
};

// ---- Header ----------------------------------------------------------

const PageHeader: React.FC<{ engaged: boolean | null; onFlip: (n: boolean) => Promise<any> }> = ({
  engaged,
  onFlip,
}) => (
  <header className="flex items-start justify-between gap-4">
    <div className="flex items-start gap-3">
      <div className="flex size-10 items-center justify-center rounded-md bg-accent-subtle text-accent-primary">
        <Sparkles className="size-5" strokeWidth={2} />
      </div>
      <div>
        <h1 className="text-heading-md font-semibold text-primary">ИИ-оркестратор</h1>
        <p className="text-body-sm text-tertiary">
          Цели · агенты · риски — система ведёт проект к результату.
        </p>
      </div>
    </div>
    {engaged !== null && (
      <Button
        variant={engaged ? "error-outline" : "secondary"}
        size="lg"
        onClick={() => onFlip(!engaged)}
        prependIcon={engaged ? <ShieldAlert /> : <Pause />}
      >
        {engaged ? "Агенты остановлены" : "Пауза агентов"}
      </Button>
    )}
  </header>
);

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
        className="flex items-center justify-center gap-2 rounded-md border border-dashed border-strong bg-layer-2 px-4 py-3 text-body-sm text-secondary transition-colors hover:border-accent-primary hover:bg-accent-subtle hover:text-accent-primary"
      >
        <Plus className="size-4" />
        Поставить цель — PLANNER декомпозирует
      </button>
    );
  }

  return (
    <Card variant={ECardVariant.WITHOUT_SHADOW} spacing={ECardSpacing.LG}>
      <form onSubmit={submit} className="flex flex-col gap-3">
        <div className="flex items-center gap-2 text-body-sm font-medium text-primary">
          <Target className="size-4 text-accent-primary" />
          Новая цель
        </div>
        <Input
          autoFocus
          required
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          placeholder="Запустить мобильное приложение к 1 июня"
          className="w-full"
        />
        <textarea
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder="Контекст: команда, бюджет, ограничения — опционально"
          rows={2}
          className="w-full rounded-md border border-strong bg-layer-2 px-3 py-2 text-body-sm text-primary placeholder:text-placeholder focus:border-accent-strong focus:outline-none"
        />
        <div className="flex items-center gap-2">
          <div className="flex items-center gap-1.5 rounded-md border border-strong bg-layer-2 px-2 py-1.5">
            <Calendar className="size-3.5 text-tertiary" />
            <input
              type="date"
              value={deadline}
              onChange={(e) => setDeadline(e.target.value)}
              className="bg-transparent text-body-xs text-primary outline-none"
            />
          </div>
          <div className="flex-1" />
          <Button variant="ghost" size="lg" onClick={() => setOpen(false)} type="button">
            Отмена
          </Button>
          <Button
            variant="primary"
            size="lg"
            type="submit"
            disabled={busy || !title.trim()}
            loading={busy}
            prependIcon={<Sparkles />}
          >
            {busy ? "PLANNER думает…" : "Сгенерировать план"}
          </Button>
        </div>
        {err && (
          <div className="flex items-center gap-1.5 text-body-xs text-danger-primary">
            <AlertTriangle className="size-3.5" />
            {err}
          </div>
        )}
      </form>
    </Card>
  );
};

// ---- Goal list -------------------------------------------------------

const GoalList: React.FC<{
  goals: Goal[] | null;
  loading: boolean;
  workspaceId: string;
  onApplied: () => void;
}> = ({ goals, loading, workspaceId, onApplied }) => {
  if (loading && !goals) {
    return (
      <Card variant={ECardVariant.WITHOUT_SHADOW} spacing={ECardSpacing.LG}>
        <div className="flex items-center justify-center gap-2 py-6 text-body-sm text-tertiary">
          <Spinner className="size-4" />
          Загружаю цели…
        </div>
      </Card>
    );
  }
  if (!goals || goals.length === 0) {
    return (
      <Card variant={ECardVariant.WITHOUT_SHADOW} spacing={ECardSpacing.LG}>
        <div className="flex flex-col items-center justify-center gap-2 py-8 text-center">
          <Target className="size-8 text-tertiary" />
          <p className="text-body-sm font-medium text-secondary">Целей пока нет</p>
          <p className="text-body-xs text-tertiary">
            Поставь первую — система соберёт план задач.
          </p>
        </div>
      </Card>
    );
  }
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
    <Card variant={ECardVariant.WITHOUT_SHADOW} spacing={ECardSpacing.SM}>
      <div className="flex items-start gap-3">
        <button
          onClick={() => setExpanded((x) => !x)}
          className="mt-0.5 text-tertiary hover:text-primary"
          aria-label={expanded ? "Свернуть" : "Развернуть"}
        >
          {expanded ? <ChevronDown className="size-4" /> : <ChevronRight className="size-4" />}
        </button>
        <div className="flex-1">
          <button
            onClick={() => setExpanded((x) => !x)}
            className="text-left text-body-sm font-medium text-primary hover:text-accent-primary"
          >
            {goal.title}
          </button>
          <div className="mt-1.5 flex flex-wrap items-center gap-2">
            <Badge variant={STATUS_BADGE[goal.status] ?? "neutral"} size="sm">
              {goal.status}
            </Badge>
            {goal.deadline && (
              <div className="flex items-center gap-1 text-caption-md text-tertiary">
                <Calendar className="size-3" />
                до {goal.deadline}
              </div>
            )}
            {epicCount > 0 && (
              <div className="flex items-center gap-1 text-caption-md text-tertiary">
                <ListChecks className="size-3" />
                {epicCount} эпиков · {taskCount} задач
              </div>
            )}
            {goal.plan_issue_count > 0 && (
              <div className="flex items-center gap-1 text-caption-md text-success-primary">
                <CheckCircle2 className="size-3" />
                создано {goal.plan_issue_count} issues
              </div>
            )}
          </div>
        </div>
        {canApply && (
          <Button
            variant="primary"
            size="base"
            onClick={onApply}
            disabled={busy}
            loading={busy}
            prependIcon={<PlayCircle />}
          >
            Применить план
          </Button>
        )}
      </div>
      {expanded && plan?.epics && (
        <div className="mt-3 space-y-3 border-t border-subtle-1 pt-3">
          {plan.summary && (
            <p className="text-body-xs italic text-tertiary">{plan.summary}</p>
          )}
          {plan.epics.map((epic: any, i: number) => (
            <div key={i} className="space-y-1">
              <div className="text-body-xs font-medium text-secondary">{epic.name}</div>
              <ul className="ml-1 space-y-0.5">
                {(epic.tasks ?? []).slice(0, 10).map((t: any, j: number) => (
                  <li key={j} className="flex items-start gap-1.5 text-body-xs text-tertiary">
                    <span className="mt-0.5 size-1 shrink-0 rounded-full bg-tertiary" />
                    <span className="text-secondary">{t.name}</span>
                    {t.estimated_hours && (
                      <span className="ml-1 text-placeholder">({t.estimated_hours} ч)</span>
                    )}
                  </li>
                ))}
                {epic.tasks?.length > 10 && (
                  <li className="ml-2.5 text-body-xs text-placeholder">
                    … ещё {epic.tasks.length - 10}
                  </li>
                )}
              </ul>
            </div>
          ))}
        </div>
      )}
    </Card>
  );
};

// ---- Risks ----------------------------------------------------------

const RiskPanel: React.FC<{ risks: Risk[]; onResolve: (id: string) => Promise<any> }> = ({
  risks,
  onResolve,
}) => (
  <Card variant={ECardVariant.WITHOUT_SHADOW} spacing={ECardSpacing.SM}>
    <div className="mb-2 flex items-center justify-between">
      <div className="flex items-center gap-2">
        <AlertTriangle className="size-4 text-warning-primary" />
        <h3 className="text-body-sm font-semibold text-primary">Открытые риски</h3>
      </div>
      <Badge variant={risks.length === 0 ? "neutral" : "warning"} size="sm">
        {risks.length}
      </Badge>
    </div>
    {risks.length === 0 ? (
      <p className="py-2 text-body-xs text-tertiary">MONITOR не видит проблем.</p>
    ) : (
      <ul className="space-y-1.5">
        {risks.slice(0, 6).map((r) => (
          <li
            key={r.id}
            className="rounded-md border border-subtle-1 bg-layer-2 p-2"
          >
            <div className="flex items-start justify-between gap-2">
              <div className="flex-1">
                <div className="flex items-center gap-1.5">
                  <Badge variant={IMPACT_BADGE[r.impact] ?? "neutral"} size="sm">
                    {r.impact}
                  </Badge>
                  <span className="text-body-xs font-medium text-secondary">
                    {r.risk_type}
                  </span>
                  <span className="text-caption-md text-placeholder">
                    {Math.round(r.confidence * 100)}%
                  </span>
                </div>
                <p className="mt-1 text-body-xs text-tertiary">{r.rationale}</p>
              </div>
              <button
                onClick={() => onResolve(r.id)}
                className="text-tertiary hover:text-success-primary"
                title="Отметить как решённый"
                aria-label="Решено"
              >
                <CheckCircle2 className="size-4" />
              </button>
            </div>
          </li>
        ))}
      </ul>
    )}
  </Card>
);

// ---- Activity feed --------------------------------------------------

const ActivityPanel: React.FC<{ actions: Action[] }> = ({ actions }) => (
  <Card
    variant={ECardVariant.WITHOUT_SHADOW}
    spacing={ECardSpacing.SM}
    className="flex min-h-0 flex-1 flex-col"
  >
    <div className="mb-2 flex items-center gap-2">
      <Activity className="size-4 text-accent-primary" />
      <h3 className="text-body-sm font-semibold text-primary">Лента агентов</h3>
    </div>
    {actions.length === 0 ? (
      <p className="py-2 text-body-xs text-tertiary">Пока тихо.</p>
    ) : (
      <ul className="flex-1 space-y-2 overflow-y-auto pr-1">
        {actions.map((a) => (
          <li key={a.id} className="border-l-2 border-subtle-1 pl-2.5">
            <div className="flex items-center gap-1.5">
              <Badge variant={AGENT_VARIANT[a.agent_type] ?? "neutral"} size="sm">
                {a.agent_type}
              </Badge>
              <span className="text-body-xs font-medium text-secondary">
                {a.action_type}
              </span>
              <Badge variant={RISK_BADGE[a.risk_level] ?? "neutral"} size="sm">
                {a.risk_level}
              </Badge>
              <span className="ml-auto flex items-center gap-1 text-caption-md text-placeholder">
                <Clock className="size-3" />
                {new Date(a.created_at).toLocaleTimeString("ru-RU", {
                  hour: "2-digit",
                  minute: "2-digit",
                })}
              </span>
            </div>
            {a.reasoning && (
              <p className="mt-0.5 truncate text-body-xs text-tertiary">{a.reasoning}</p>
            )}
          </li>
        ))}
      </ul>
    )}
  </Card>
);

export default OrchestratorPage;
