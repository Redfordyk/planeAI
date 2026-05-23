/**
 * AgentActivityFeed — TZ 5.6 transparency UI.
 *
 * The feed component answers the question the TZ frames as the
 * condition for accepting an autonomous agent: "what did it do, on
 * which task, why, and can I take it back?".
 *
 * Layout: filters bar on top, paginated action list below. Each row
 * shows task / tool / time / rationale plus — for ``set_labels`` —
 * an "Undo" button (only one tool is currently reversible per TZ
 * 5.6, with the snapshot stored in ``output.previous_label_ids``).
 *
 * Styling matches the Plane palette (`bg-custom-*`, `text-custom-*`)
 * for consistency with `<AISearch>` from TZ 2.6.
 */

import { useCallback, useState } from "react";

import {
  AgentAction,
  AgentActionStatus,
  AgentFeedFilters,
  undoAction,
  useAgentFeed,
} from "../../hooks/ai/use-agent-feed";

export type AgentActivityFeedProps = {
  workspaceId: string;
  workspaceSlug: string;
  /** Optional pre-applied filter — e.g. mounting on a project page. */
  initialFilters?: AgentFeedFilters;
  /** Optional project options for the project-filter dropdown.
   *  Pass `[]` to hide the dropdown entirely. */
  projects?: { id: string; name: string }[];
  className?: string;
};

const TOOL_OPTIONS: { value: string; label: string }[] = [
  { value: "", label: "Все инструменты" },
  { value: "set_priority", label: "Приоритет" },
  { value: "set_labels", label: "Метки" },
  { value: "suggest_assignee", label: "Предложение исполнителя" },
  { value: "add_comment", label: "Комментарий" },
  { value: "update_description", label: "Описание" },
  { value: "find_work_items", label: "Поиск похожих" },
];

const STATUS_OPTIONS: { value: AgentActionStatus | ""; label: string }[] = [
  { value: "", label: "Все статусы" },
  { value: "applied", label: "Выполнено" },
  { value: "rejected", label: "Отклонено" },
  { value: "error", label: "Ошибка" },
];

export function AgentActivityFeed({
  workspaceId,
  workspaceSlug,
  initialFilters,
  projects,
  className = "",
}: AgentActivityFeedProps) {
  const {
    data,
    error,
    loading,
    page,
    setPage,
    filters,
    setFilters,
    refresh,
    patchAction,
  } = useAgentFeed(workspaceId, initialFilters ?? {});
  const [undoErr, setUndoErr] = useState<string | null>(null);

  const onUndo = useCallback(
    async (action: AgentAction) => {
      setUndoErr(null);
      try {
        const updated = await undoAction(workspaceId, action.id);
        // Local optimistic patch so the row updates without a refetch.
        patchAction(action.id, updated);
      } catch (e) {
        setUndoErr((e as Error).message);
      }
    },
    [workspaceId, patchAction]
  );

  const totalPages = data ? Math.max(1, Math.ceil(data.count / data.page_size)) : 1;

  return (
    <div className={`flex flex-col gap-3 ${className}`}>
      <Filters
        filters={filters}
        setFilters={setFilters}
        projects={projects}
      />

      {error && (
        <ErrorBanner message={error} onRetry={refresh} />
      )}
      {undoErr && (
        <div className="rounded border border-red-300 bg-red-50 px-3 py-2 text-xs text-red-700">
          Не удалось отменить действие: {undoErr}
        </div>
      )}

      <div className="overflow-hidden rounded border border-custom-border-200">
        <table className="w-full text-sm">
          <thead className="bg-custom-background-90 text-xs uppercase text-custom-text-300">
            <tr>
              <th className="px-3 py-2 text-left">Когда</th>
              <th className="px-3 py-2 text-left">Задача</th>
              <th className="px-3 py-2 text-left">Действие</th>
              <th className="px-3 py-2 text-left">Обоснование</th>
              <th className="px-3 py-2 text-left">Статус</th>
              <th className="px-3 py-2 text-right">Отмена</th>
            </tr>
          </thead>
          <tbody>
            {loading && !data && (
              <tr>
                <td colSpan={6} className="px-3 py-6 text-center text-custom-text-300">
                  Загрузка ленты действий…
                </td>
              </tr>
            )}
            {data && data.results.length === 0 && (
              <tr>
                <td colSpan={6} className="px-3 py-6 text-center text-custom-text-300">
                  Действий пока нет.
                </td>
              </tr>
            )}
            {data?.results.map((a) => (
              <ActionRow
                key={a.id}
                action={a}
                workspaceSlug={workspaceSlug}
                onUndo={() => onUndo(a)}
              />
            ))}
          </tbody>
        </table>
      </div>

      {data && data.count > data.page_size && (
        <Pagination
          page={page}
          totalPages={totalPages}
          loading={loading}
          onChange={setPage}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Filters bar
// ---------------------------------------------------------------------------

function Filters({
  filters,
  setFilters,
  projects,
}: {
  filters: AgentFeedFilters;
  setFilters: (f: AgentFeedFilters) => void;
  projects?: { id: string; name: string }[];
}) {
  return (
    <div className="flex flex-wrap items-center gap-2">
      {projects && projects.length > 0 && (
        <select
          value={filters.project ?? ""}
          onChange={(e) =>
            setFilters({ ...filters, project: e.target.value || null })
          }
          className="rounded border border-custom-border-200 bg-custom-background-90 px-2 py-1 text-xs"
        >
          <option value="">Все проекты</option>
          {projects.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
      )}
      <select
        value={filters.tool ?? ""}
        onChange={(e) => setFilters({ ...filters, tool: e.target.value || null })}
        className="rounded border border-custom-border-200 bg-custom-background-90 px-2 py-1 text-xs"
      >
        {TOOL_OPTIONS.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
      <select
        value={filters.status ?? ""}
        onChange={(e) =>
          setFilters({
            ...filters,
            status: (e.target.value || null) as AgentActionStatus | null,
          })
        }
        className="rounded border border-custom-border-200 bg-custom-background-90 px-2 py-1 text-xs"
      >
        {STATUS_OPTIONS.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Action row
// ---------------------------------------------------------------------------

function ActionRow({
  action,
  workspaceSlug,
  onUndo,
}: {
  action: AgentAction;
  workspaceSlug: string;
  onUndo: () => void;
}) {
  const issueHref = `/${workspaceSlug}/browse/${action.issue_id}`;
  return (
    <tr className="border-t border-custom-border-200 align-top">
      <td className="px-3 py-2 text-xs text-custom-text-300 whitespace-nowrap">
        {formatTime(action.created_at)}
      </td>
      <td className="px-3 py-2">
        <a
          href={issueHref}
          className="text-custom-primary-100 hover:underline"
          target="_blank"
          rel="noreferrer"
        >
          {action.issue_id.slice(0, 8)}
        </a>
      </td>
      <td className="px-3 py-2">
        <ToolBadge tool={action.tool_name} />
      </td>
      <td className="px-3 py-2 text-custom-text-200">
        <div>{action.rationale}</div>
        {action.error && (
          <div className="mt-0.5 text-xs text-red-600">{action.error}</div>
        )}
      </td>
      <td className="px-3 py-2">
        <StatusPill status={action.status} undone={Boolean(action.undone_at)} />
      </td>
      <td className="px-3 py-2 text-right">
        {action.reversible ? (
          <button
            type="button"
            onClick={onUndo}
            className="rounded bg-custom-background-80 px-2 py-1 text-xs hover:bg-custom-background-70"
          >
            Отменить
          </button>
        ) : action.undone_at ? (
          <span className="text-xs text-custom-text-300">отменено</span>
        ) : (
          <span className="text-xs text-custom-text-300">—</span>
        )}
      </td>
    </tr>
  );
}

function ToolBadge({ tool }: { tool: string }) {
  return (
    <span className="rounded bg-custom-background-80 px-2 py-0.5 text-xs">
      {TOOL_OPTIONS.find((o) => o.value === tool)?.label ?? tool}
    </span>
  );
}

function StatusPill({
  status,
  undone,
}: {
  status: AgentActionStatus;
  undone: boolean;
}) {
  if (undone) {
    return (
      <span className="rounded bg-custom-background-80 px-2 py-0.5 text-xs text-custom-text-300">
        Отменено
      </span>
    );
  }
  if (status === "applied") {
    return (
      <span className="rounded bg-green-50 px-2 py-0.5 text-xs text-green-700">
        Выполнено
      </span>
    );
  }
  if (status === "rejected") {
    return (
      <span className="rounded bg-yellow-50 px-2 py-0.5 text-xs text-yellow-800">
        Отклонено
      </span>
    );
  }
  return (
    <span className="rounded bg-red-50 px-2 py-0.5 text-xs text-red-700">
      Ошибка
    </span>
  );
}

// ---------------------------------------------------------------------------
// Pagination
// ---------------------------------------------------------------------------

function Pagination({
  page,
  totalPages,
  loading,
  onChange,
}: {
  page: number;
  totalPages: number;
  loading: boolean;
  onChange: (p: number) => void;
}) {
  return (
    <div className="flex items-center justify-end gap-2 text-xs text-custom-text-300">
      <button
        type="button"
        disabled={loading || page <= 1}
        onClick={() => onChange(page - 1)}
        className="rounded border border-custom-border-200 px-2 py-1 disabled:opacity-50"
      >
        ‹
      </button>
      <span>
        {page} / {totalPages}
      </span>
      <button
        type="button"
        disabled={loading || page >= totalPages}
        onClick={() => onChange(page + 1)}
        className="rounded border border-custom-border-200 px-2 py-1 disabled:opacity-50"
      >
        ›
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Misc
// ---------------------------------------------------------------------------

function ErrorBanner({
  message,
  onRetry,
}: {
  message: string;
  onRetry: () => void;
}) {
  return (
    <div className="flex items-center justify-between rounded border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-700">
      <span>{message}</span>
      <button
        type="button"
        onClick={onRetry}
        className="rounded bg-red-100 px-2 py-1 text-xs hover:bg-red-200"
      >
        Повторить
      </button>
    </div>
  );
}

function formatTime(iso: string): string {
  try {
    const d = new Date(iso);
    return d.toLocaleString("ru-RU", {
      day: "2-digit",
      month: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}
