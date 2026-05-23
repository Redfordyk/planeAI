/**
 * TZ 6.3 — hook for the AI usage dashboard.
 *
 * One small hook layered on GET /api/ai/workspaces/<id>/usage/stats/.
 * Matches the response shape declared in ai/usage_views.py exactly
 * — when the backend serializer changes, this file changes with it.
 *
 * The endpoint is workspace-**admin** only (cost data is sensitive),
 * so a 403 here is expected for non-admin members and the dashboard
 * component renders a permission-denied banner rather than a generic
 * error.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

// ---------------------------------------------------------------------------
// Types — mirror ai/usage.py:compute_usage_stats + ai/usage_views.py
// ---------------------------------------------------------------------------

export type UsageFeature =
  | "intent_search"
  | "summarize"
  | "bulk"
  | "agent"
  | "embed";

export type UsageBudgetLevel = "ok" | "warning" | "critical" | "exceeded" | "unset";

export type UsageTotals = {
  calls: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_creation_tokens: number;
  billable_tokens: number;
  /** Decimal serialised as a string — JS numbers would lose precision. */
  cost_usd: string;
};

export type UsageByFeatureRow = {
  feature: UsageFeature;
  calls: number;
  billable_tokens: number;
  cost_usd: string;
};

export type UsageByModelRow = {
  model: string;
  calls: number;
  billable_tokens: number;
  cost_usd: string;
};

export type UsageByUserRow = {
  /** null when the underlying user has been deleted (SET_NULL). */
  user_id: string | null;
  calls: number;
  billable_tokens: number;
  cost_usd: string;
};

export type UsageByDayRow = {
  /** ISO date string ``YYYY-MM-DD``. */
  date: string;
  calls: number;
  billable_tokens: number;
  cost_usd: string;
};

export type UsageBudgetPanel = {
  tokens_used: number;
  tokens_budget: number;
  ratio: number;
  exceeded: boolean;
  level: UsageBudgetLevel;
};

export type UsageStats = {
  period: { start: string; end: string };
  totals: UsageTotals;
  by_feature: UsageByFeatureRow[];
  by_model: UsageByModelRow[];
  by_user: UsageByUserRow[];
  by_day: UsageByDayRow[];
  budget: UsageBudgetPanel;
};

export type UsagePeriod = {
  /** ISO-8601 with TZ — JS ``Date.toISOString()`` works. */
  from: string;
  to: string;
};

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

export type UseUsageStatsResult = {
  data: UsageStats | null;
  loading: boolean;
  /** Plain text, ready for the error banner. */
  error: string | null;
  /** True when the server returned 403 — UI distinguishes this from
   *  a generic error to show "admin-only" copy instead of "retry". */
  forbidden: boolean;
  period: UsagePeriod | null;
  setPeriod: (p: UsagePeriod | null) => void;
  refresh: () => Promise<void>;
};

export function useUsageStats(
  workspaceId: string,
  initialPeriod: UsagePeriod | null = null,
  topUsers: number = 10
): UseUsageStatsResult {
  const [data, setData] = useState<UsageStats | null>(null);
  const [period, setPeriod] = useState<UsagePeriod | null>(initialPeriod);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [forbidden, setForbidden] = useState(false);
  const aliveRef = useRef(true);

  const fetchOnce = useCallback(async () => {
    setLoading(true);
    setForbidden(false);
    try {
      const sp = new URLSearchParams();
      if (period) {
        sp.set("from", period.from);
        sp.set("to", period.to);
      }
      sp.set("top_users", String(topUsers));
      const resp = await fetch(
        `/api/ai/workspaces/${workspaceId}/usage/stats/?${sp.toString()}`,
        { credentials: "same-origin" }
      );
      if (!resp.ok) {
        if (resp.status === 403) {
          if (!aliveRef.current) return;
          setForbidden(true);
          setError(null);
          return;
        }
        const body: { error?: string } = await resp.json().catch(() => ({}));
        if (!aliveRef.current) return;
        setError(body.error || `HTTP ${resp.status}`);
        return;
      }
      const json = (await resp.json()) as UsageStats;
      if (!aliveRef.current) return;
      setData(json);
      setError(null);
    } catch (e) {
      if (!aliveRef.current) return;
      setError((e as Error).message || "network error");
    } finally {
      if (aliveRef.current) setLoading(false);
    }
  }, [workspaceId, period, topUsers]);

  useEffect(() => {
    aliveRef.current = true;
    fetchOnce();
    return () => {
      aliveRef.current = false;
    };
  }, [fetchOnce]);

  return { data, loading, error, forbidden, period, setPeriod, refresh: fetchOnce };
}

// ---------------------------------------------------------------------------
// Period presets — the dashboard uses these for one-click filters.
// ---------------------------------------------------------------------------

export type PeriodPresetKey = "this_month" | "last_30_days" | "last_7_days" | "last_month";

export function periodPreset(key: PeriodPresetKey, now: Date = new Date()): UsagePeriod {
  const end = new Date(now);
  // Round end up to next minute so the "this instant" row is included.
  end.setSeconds(0, 0);
  end.setMinutes(end.getMinutes() + 1);
  let start: Date;
  if (key === "this_month") {
    start = new Date(now.getFullYear(), now.getMonth(), 1, 0, 0, 0, 0);
  } else if (key === "last_month") {
    start = new Date(now.getFullYear(), now.getMonth() - 1, 1, 0, 0, 0, 0);
    end.setFullYear(now.getFullYear(), now.getMonth(), 1);
    end.setHours(0, 0, 0, 0);
  } else if (key === "last_7_days") {
    start = new Date(now);
    start.setDate(start.getDate() - 7);
    start.setHours(0, 0, 0, 0);
  } else {
    start = new Date(now);
    start.setDate(start.getDate() - 30);
    start.setHours(0, 0, 0, 0);
  }
  return { from: start.toISOString(), to: end.toISOString() };
}

// ---------------------------------------------------------------------------
// Display helpers — kept here so the dashboard component stays UI-focused.
// ---------------------------------------------------------------------------

export const FEATURE_LABELS: Record<UsageFeature, string> = {
  intent_search: "Поиск",
  summarize: "Саммари",
  bulk: "Bulk-операции",
  agent: "Агент",
  embed: "Эмбеддинги",
};

/** Decimal-string → `"$1.23"` (or `"$1,234.56"` for big sums). */
export function formatUsd(decimalStr: string): string {
  const n = Number(decimalStr);
  if (!Number.isFinite(n)) return "$—";
  if (n === 0) return "$0";
  // Sub-cent granularity for tiny numbers; group separators for big ones.
  const opts: Intl.NumberFormatOptions = n < 1
    ? { minimumFractionDigits: 4, maximumFractionDigits: 4 }
    : { minimumFractionDigits: 2, maximumFractionDigits: 2 };
  return "$" + new Intl.NumberFormat("en-US", opts).format(n);
}

/** 1234567 → "1.2M", 1234 → "1.2K". Tokens scale a lot. */
export function formatTokens(n: number): string {
  if (n === 0) return "0";
  if (Math.abs(n) >= 1_000_000)
    return (n / 1_000_000).toFixed(1).replace(/\.0$/, "") + "M";
  if (Math.abs(n) >= 1_000)
    return (n / 1_000).toFixed(1).replace(/\.0$/, "") + "K";
  return String(n);
}

/** Sum cost across `by_feature` rows — useful client-side to derive a
 *  share-of-total for the percentage column. */
export function totalCostFromFeatures(rows: UsageByFeatureRow[]): number {
  return rows.reduce((acc, r) => acc + Number(r.cost_usd), 0);
}

/** Build a stable percentage chart data series. Memoise in the
 *  caller — this is intentionally a plain function. */
export function featureSharePct(rows: UsageByFeatureRow[]): { feature: UsageFeature; pct: number }[] {
  const total = totalCostFromFeatures(rows);
  if (total <= 0) return rows.map((r) => ({ feature: r.feature, pct: 0 }));
  return rows.map((r) => ({
    feature: r.feature,
    pct: (Number(r.cost_usd) / total) * 100,
  }));
}
