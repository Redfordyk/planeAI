/**
 * UsageDashboard — TZ 6.3 admin dashboard for AI spend.
 *
 * Renders:
 *
 *   - Total cost ($, tokens) vs monthly budget, with a coloured
 *     progress bar that mirrors the Prometheus alert thresholds:
 *     green < 80%, yellow ≥ 80%, orange ≥ 95%, red when exceeded.
 *   - Breakdown by feature (intent_search / summarize / bulk / agent /
 *     embed) — bar chart driven by ``share_pct``.
 *   - Top 10 users by cost.
 *   - Daily trend (sparkline-ish bar chart, no chart lib required).
 *
 * Period filter: this month (default), last month, last 30 days,
 * last 7 days. The custom-range picker is deliberately *not* wired
 * here — keeping the surface narrow lets us ship the dashboard on
 * MVP day; the backend already accepts arbitrary ranges via ``from``
 * / ``to`` (UsageStatsView in ai/usage_views.py).
 *
 * Styling mirrors ``AgentActivityFeed`` from TZ 5.6:
 * ``bg-custom-background-*`` / ``text-custom-text-*`` so the page
 * inherits Plane's theme tokens (light + dark + custom themes all
 * work without a per-component branch).
 *
 * The component renders zero chart-library imports on purpose. We
 * use semantic ``<div>`` widths driven by inline ``style.width``,
 * which is enough for the four small charts the TZ asks for and
 * keeps the bundle and the SSR story simple.
 */

import { useMemo, useState } from "react";

import {
  FEATURE_LABELS,
  PeriodPresetKey,
  UsageBudgetPanel,
  UsageByDayRow,
  UsageByFeatureRow,
  UsageByModelRow,
  UsageByUserRow,
  UsagePeriod,
  formatTokens,
  formatUsd,
  periodPreset,
  totalCostFromFeatures,
  useUsageStats,
} from "../../hooks/ai/use-usage-stats";

export type UsageDashboardProps = {
  workspaceId: string;
  className?: string;
  /** Optional user lookup so the "top users" table can show emails.
   *  Pass a map ``{[user_id]: email}`` from the parent's existing
   *  user store. Missing entries fall back to a shortened uuid. */
  userEmails?: Record<string, string>;
};

const PRESETS: { key: PeriodPresetKey; label: string }[] = [
  { key: "this_month", label: "Этот месяц" },
  { key: "last_month", label: "Прошлый месяц" },
  { key: "last_30_days", label: "30 дней" },
  { key: "last_7_days", label: "7 дней" },
];

export function UsageDashboard({
  workspaceId,
  className = "",
  userEmails,
}: UsageDashboardProps) {
  // Default to "this month" — same window the budget guard cares
  // about. ``initialPeriod=null`` would also work (the server
  // defaults the same way), but rendering the active-preset chip
  // requires us to know which one is selected.
  const [presetKey, setPresetKey] = useState<PeriodPresetKey>("this_month");
  const initialPeriod = useMemo<UsagePeriod>(
    () => periodPreset("this_month"),
    []
  );
  const { data, loading, error, forbidden, setPeriod, refresh } = useUsageStats(
    workspaceId,
    initialPeriod
  );

  const onPreset = (key: PeriodPresetKey) => {
    setPresetKey(key);
    setPeriod(periodPreset(key));
  };

  if (forbidden) {
    return (
      <section
        className={`rounded border border-custom-border-200 bg-custom-background-90 p-6 text-sm text-custom-text-300 ${className}`}
      >
        Доступ к расходу токенов есть только у администраторов
        воркспейса.
      </section>
    );
  }

  return (
    <div className={`flex flex-col gap-5 ${className}`}>
      <Toolbar
        presetKey={presetKey}
        onPreset={onPreset}
        loading={loading}
        onRefresh={refresh}
      />

      {error && (
        <div className="flex items-center justify-between rounded border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-700">
          <span>{error}</span>
          <button
            type="button"
            onClick={refresh}
            className="rounded bg-red-100 px-2 py-1 text-xs hover:bg-red-200"
          >
            Повторить
          </button>
        </div>
      )}

      {!data && loading && (
        <div className="rounded border border-custom-border-200 bg-custom-background-90 p-6 text-center text-sm text-custom-text-300">
          Загрузка статистики…
        </div>
      )}

      {data && (
        <>
          <BudgetCard
            budget={data.budget}
            totalCost={data.totals.cost_usd}
            calls={data.totals.calls}
          />

          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            <ByFeatureCard rows={data.by_feature} />
            <TopUsersCard rows={data.by_user} emails={userEmails ?? {}} />
          </div>

          <ByDayCard rows={data.by_day} />

          <ByModelCard rows={data.by_model} />
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Toolbar
// ---------------------------------------------------------------------------

function Toolbar({
  presetKey,
  onPreset,
  loading,
  onRefresh,
}: {
  presetKey: PeriodPresetKey;
  onPreset: (k: PeriodPresetKey) => void;
  loading: boolean;
  onRefresh: () => void;
}) {
  return (
    <div className="flex flex-wrap items-center gap-2">
      <div className="flex items-center gap-1">
        {PRESETS.map((p) => (
          <button
            key={p.key}
            type="button"
            onClick={() => onPreset(p.key)}
            className={
              "rounded px-2 py-1 text-xs " +
              (presetKey === p.key
                ? "bg-custom-primary-100 text-white"
                : "bg-custom-background-80 text-custom-text-200 hover:bg-custom-background-70")
            }
          >
            {p.label}
          </button>
        ))}
      </div>
      <button
        type="button"
        onClick={onRefresh}
        disabled={loading}
        className="ml-auto rounded border border-custom-border-200 px-2 py-1 text-xs text-custom-text-200 hover:bg-custom-background-80 disabled:opacity-50"
      >
        {loading ? "Обновление…" : "Обновить"}
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Budget card — total cost + progress vs limit
// ---------------------------------------------------------------------------

function BudgetCard({
  budget,
  totalCost,
  calls,
}: {
  budget: UsageBudgetPanel;
  totalCost: string;
  calls: number;
}) {
  // Width clamp: ratio can be >1 right after the hard-cap fires (the
  // last sneaked-in row pushed us slightly past). UI clamps to 100%
  // but the text still shows the real percentage so the operator
  // sees the actual overshoot.
  const widthPct = Math.min(100, Math.max(0, budget.ratio * 100));
  const pctLabel =
    budget.tokens_budget > 0
      ? Math.round(budget.ratio * 100) + "%"
      : "—";

  const barClass =
    budget.level === "exceeded"
      ? "bg-red-500"
      : budget.level === "critical"
        ? "bg-orange-500"
        : budget.level === "warning"
          ? "bg-yellow-500"
          : budget.level === "unset"
            ? "bg-custom-background-70"
            : "bg-green-500";

  return (
    <section className="flex flex-col gap-3 rounded border border-custom-border-200 bg-custom-background-90 p-4">
      <header className="flex items-baseline justify-between gap-4">
        <div>
          <div className="text-xs uppercase tracking-wide text-custom-text-300">
            Расход за период
          </div>
          <div className="text-2xl font-medium text-custom-text-100">
            {formatUsd(totalCost)}
          </div>
          <div className="text-xs text-custom-text-300">
            {calls.toLocaleString("ru-RU")} вызовов
          </div>
        </div>
        <div className="text-right">
          <div className="text-xs uppercase tracking-wide text-custom-text-300">
            Использовано токенов
          </div>
          <div className="text-lg text-custom-text-100">
            {budget.tokens_used.toLocaleString("ru-RU")}
            {budget.tokens_budget > 0 && (
              <span className="text-sm text-custom-text-300">
                {" "}
                / {budget.tokens_budget.toLocaleString("ru-RU")}
              </span>
            )}
          </div>
          <div className="text-xs text-custom-text-300">{pctLabel}</div>
        </div>
      </header>

      <div className="h-2 w-full overflow-hidden rounded bg-custom-background-80">
        <div
          className={`h-full transition-all ${barClass}`}
          style={{ width: `${widthPct}%` }}
        />
      </div>

      {budget.exceeded && (
        <div className="text-xs text-red-600">
          Бюджет исчерпан — новые ИИ-вызовы возвращают 429 до начала
          следующего месяца или повышения лимита.
        </div>
      )}
      {!budget.exceeded && budget.level === "critical" && (
        <div className="text-xs text-orange-600">
          Достигнут порог 95% — алерт уже сработал. Если расход
          продолжит расти, дайте +лимит или временно отключите фичу.
        </div>
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// By feature
// ---------------------------------------------------------------------------

function ByFeatureCard({ rows }: { rows: UsageByFeatureRow[] }) {
  const total = totalCostFromFeatures(rows);
  // Pre-sort by cost desc so the top consumer is always at the top.
  const sorted = useMemo(
    () => [...rows].sort((a, b) => Number(b.cost_usd) - Number(a.cost_usd)),
    [rows]
  );
  return (
    <section className="flex flex-col gap-3 rounded border border-custom-border-200 bg-custom-background-90 p-4">
      <h3 className="text-sm font-medium text-custom-text-200">
        Расход по фичам
      </h3>
      {sorted.every((r) => Number(r.cost_usd) === 0) ? (
        <div className="text-xs text-custom-text-300">
          За выбранный период расходов нет.
        </div>
      ) : (
        <ul className="flex flex-col gap-2">
          {sorted.map((r) => {
            const pct = total > 0 ? (Number(r.cost_usd) / total) * 100 : 0;
            return (
              <li key={r.feature} className="flex flex-col gap-1">
                <div className="flex items-baseline justify-between text-xs text-custom-text-200">
                  <span>{FEATURE_LABELS[r.feature]}</span>
                  <span className="text-custom-text-300">
                    {formatUsd(r.cost_usd)} ·{" "}
                    {formatTokens(r.billable_tokens)} · {pct.toFixed(0)}%
                  </span>
                </div>
                <div className="h-1.5 w-full overflow-hidden rounded bg-custom-background-80">
                  <div
                    className="h-full bg-custom-primary-100"
                    style={{ width: `${pct}%` }}
                  />
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Top users
// ---------------------------------------------------------------------------

function TopUsersCard({
  rows,
  emails,
}: {
  rows: UsageByUserRow[];
  emails: Record<string, string>;
}) {
  return (
    <section className="flex flex-col gap-3 rounded border border-custom-border-200 bg-custom-background-90 p-4">
      <h3 className="text-sm font-medium text-custom-text-200">
        Топ пользователей по расходу
      </h3>
      {rows.length === 0 ? (
        <div className="text-xs text-custom-text-300">
          Никто пока не пользовался ИИ.
        </div>
      ) : (
        <table className="w-full text-xs">
          <thead className="text-custom-text-300">
            <tr>
              <th className="py-1 text-left font-normal">Пользователь</th>
              <th className="py-1 text-right font-normal">Вызовов</th>
              <th className="py-1 text-right font-normal">Токенов</th>
              <th className="py-1 text-right font-normal">$</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.user_id ?? "deleted"} className="border-t border-custom-border-200">
                <td className="py-1.5 text-custom-text-200">
                  {r.user_id == null
                    ? <span className="italic text-custom-text-300">(удалённый пользователь)</span>
                    : (emails[r.user_id] ?? `…${r.user_id.slice(-8)}`)}
                </td>
                <td className="py-1.5 text-right text-custom-text-200">
                  {r.calls.toLocaleString("ru-RU")}
                </td>
                <td className="py-1.5 text-right text-custom-text-200">
                  {formatTokens(r.billable_tokens)}
                </td>
                <td className="py-1.5 text-right text-custom-text-100">
                  {formatUsd(r.cost_usd)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Daily trend
// ---------------------------------------------------------------------------

function ByDayCard({ rows }: { rows: UsageByDayRow[] }) {
  // Tallest bar = max cost across the window; everything else is
  // relative. Empty window → render the date axis with zero bars so
  // the admin still sees the period covered.
  const maxCost = useMemo(
    () => rows.reduce((m, r) => Math.max(m, Number(r.cost_usd)), 0),
    [rows]
  );
  return (
    <section className="flex flex-col gap-3 rounded border border-custom-border-200 bg-custom-background-90 p-4">
      <h3 className="text-sm font-medium text-custom-text-200">
        Расход по дням
      </h3>
      <div className="flex items-end gap-1 overflow-x-auto pb-2" style={{ height: 120 }}>
        {rows.map((r) => {
          const cost = Number(r.cost_usd);
          const h = maxCost > 0 ? (cost / maxCost) * 100 : 0;
          // tabular-num so the tooltip alignment doesn't jiggle in
          // some browsers; title shows the precise day + cost.
          return (
            <div
              key={r.date}
              className="flex flex-col items-center"
              style={{ width: 18 }}
              title={`${r.date}\n${formatUsd(r.cost_usd)} · ${formatTokens(r.billable_tokens)} токенов · ${r.calls} вызовов`}
            >
              <div className="flex w-full items-end" style={{ height: 100 }}>
                <div
                  className="w-full rounded-sm bg-custom-primary-100"
                  style={{ height: `${h}%`, minHeight: cost > 0 ? 2 : 0 }}
                />
              </div>
              <div className="mt-1 select-none text-[10px] text-custom-text-300">
                {/* day-of-month only; full date in tooltip */}
                {r.date.slice(-2)}
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// By model
// ---------------------------------------------------------------------------

function ByModelCard({ rows }: { rows: UsageByModelRow[] }) {
  if (rows.length === 0) return null;
  return (
    <section className="flex flex-col gap-3 rounded border border-custom-border-200 bg-custom-background-90 p-4">
      <h3 className="text-sm font-medium text-custom-text-200">
        Расход по моделям
      </h3>
      <table className="w-full text-xs">
        <thead className="text-custom-text-300">
          <tr>
            <th className="py-1 text-left font-normal">Модель</th>
            <th className="py-1 text-right font-normal">Вызовов</th>
            <th className="py-1 text-right font-normal">Токенов</th>
            <th className="py-1 text-right font-normal">$</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.model} className="border-t border-custom-border-200">
              <td className="py-1.5 text-custom-text-200 font-mono">{r.model}</td>
              <td className="py-1.5 text-right text-custom-text-200">
                {r.calls.toLocaleString("ru-RU")}
              </td>
              <td className="py-1.5 text-right text-custom-text-200">
                {formatTokens(r.billable_tokens)}
              </td>
              <td className="py-1.5 text-right text-custom-text-100">
                {formatUsd(r.cost_usd)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
