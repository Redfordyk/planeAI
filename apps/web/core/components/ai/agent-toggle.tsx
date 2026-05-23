/**
 * AgentToggle — admin-only switch for AIAgent.enabled.
 *
 * Pairs with the TZ 5.6 PATCH /agents/<id>/ endpoint. Read access to
 * the list is open to every workspace member (the toggle renders
 * disabled for non-admins, with a tooltip), but the write call is
 * gated server-side — the backend returns 403 if the caller isn't
 * an admin. We surface that 403 as an inline error so a non-admin
 * trying anyway gets a clear "admin only" message rather than the
 * silent failure of a disabled button.
 *
 * The component is data-driven: pass `isAdmin` from the caller's
 * existing role state. Plane's web app exposes the workspace role
 * via `useUser()` / `useWorkspaces()`; we deliberately don't import
 * those here to keep this component portable across mount points
 * (settings page, sidebar widget, etc.).
 */

import { useState } from "react";

import { AgentRow, useAgents } from "../../hooks/ai/use-agent-feed";

export type AgentToggleProps = {
  workspaceId: string;
  isAdmin: boolean;
  /** Optional render override if the caller wants a different shell. */
  className?: string;
};

export function AgentToggle({
  workspaceId,
  isAdmin,
  className = "",
}: AgentToggleProps) {
  const { agents, loading, error, setEnabled, refresh } = useAgents(workspaceId);
  const [busy, setBusy] = useState<string | null>(null);
  const [mutErr, setMutErr] = useState<string | null>(null);

  const onToggle = async (agent: AgentRow) => {
    if (!isAdmin) return;
    setBusy(agent.id);
    setMutErr(null);
    try {
      await setEnabled(agent.id, !agent.enabled);
    } catch (e) {
      setMutErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  if (loading && agents.length === 0) {
    return (
      <div className={`text-sm text-custom-text-300 ${className}`}>
        Загрузка состояния агента…
      </div>
    );
  }
  if (error) {
    return (
      <div className={`text-sm text-red-700 ${className}`}>
        Не удалось загрузить агентов: {error}{" "}
        <button
          type="button"
          onClick={refresh}
          className="ml-2 underline"
        >
          повторить
        </button>
      </div>
    );
  }
  if (agents.length === 0) {
    return (
      <div className={`text-sm text-custom-text-300 ${className}`}>
        В этом воркспейсе ещё нет ИИ-агентов.
      </div>
    );
  }

  return (
    <div className={`flex flex-col gap-2 ${className}`}>
      {mutErr && (
        <div className="rounded border border-red-300 bg-red-50 px-3 py-2 text-xs text-red-700">
          {mutErr}
        </div>
      )}
      {agents.map((a) => (
        <AgentRowView
          key={a.id}
          agent={a}
          isAdmin={isAdmin}
          busy={busy === a.id}
          onToggle={() => onToggle(a)}
        />
      ))}
    </div>
  );
}

function AgentRowView({
  agent,
  isAdmin,
  busy,
  onToggle,
}: {
  agent: AgentRow;
  isAdmin: boolean;
  busy: boolean;
  onToggle: () => void;
}) {
  return (
    <div className="flex items-center justify-between rounded border border-custom-border-200 bg-custom-background-90 px-3 py-2">
      <div className="flex flex-col">
        <div className="text-sm text-custom-text-100">
          🤖 {agent.user_email || "ИИ-агент"}
        </div>
        <div className="text-xs text-custom-text-300">
          {agent.enabled ? "Активен" : "Выключен"}
        </div>
      </div>
      <button
        type="button"
        disabled={!isAdmin || busy}
        onClick={onToggle}
        title={
          isAdmin
            ? agent.enabled
              ? "Выключить агента"
              : "Включить агента"
            : "Переключение доступно только администратору воркспейса"
        }
        className={`relative inline-flex h-5 w-9 items-center rounded-full transition-colors ${
          agent.enabled ? "bg-custom-primary-100" : "bg-custom-background-80"
        } ${!isAdmin || busy ? "cursor-not-allowed opacity-60" : "cursor-pointer"}`}
      >
        <span
          className={`inline-block h-4 w-4 transform rounded-full bg-white shadow transition-transform ${
            agent.enabled ? "translate-x-4" : "translate-x-0.5"
          }`}
        />
      </button>
    </div>
  );
}
