"""Single chokepoint for writing ``AIUsageLog`` rows + read-side
aggregations consumed by the budget guard, monitoring metrics, and
the TZ 6.3 usage dashboard.

Every feature path that talks to Anthropic or OpenAI must funnel its
billing through ``record_usage`` so that:

  - costs are computed from one pricing table (``ai.pricing``);
  - cache-read / cache-creation tokens are accounted for separately
    (Anthropic charges them at lower / higher rates than fresh input);
  - the per-workspace monthly budget guard (``ai.guards``) has a
    consistent denominator.

Async callers (TZ 2.3 SSE streaming) MUST wrap the call in
``asgiref.sync.sync_to_async`` — this function does an ORM write,
which is the canonical sync-only path Django warns about in async
contexts. See STREAMING.md.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from django.db.models import Count, F, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

from ai.models import AIUsageLog, WorkspaceAIConfig
from ai.pricing import chat_cost, embed_cost


logger = logging.getLogger("plane.ai.usage")


def _usage_field(usage: Any, *names: str, default: int = 0) -> int:
    """Read an attribute or dict key from `usage`, tolerating either
    shape (Anthropic SDK returns objects, OpenAI returns objects with
    `.total_tokens`, and tests sometimes pass plain dicts)."""
    for name in names:
        if isinstance(usage, dict):
            v = usage.get(name)
        else:
            v = getattr(usage, name, None)
        if v is not None:
            return int(v)
    return default


def record_usage(
    *,
    workspace_id,
    user_id,
    feature: str,
    model: str,
    usage: Any,
) -> Decimal:
    """Write one AIUsageLog row, return computed USD cost.

    `usage` can be any of:
      - anthropic.types.Usage  (attrs: input_tokens, output_tokens,
        cache_read_input_tokens, cache_creation_input_tokens)
      - openai.types.CompletionUsage / EmbeddingUsage
        (attrs: total_tokens, input_tokens for embeddings)
      - dict with the same keys (for tests)
    """
    input_tokens = _usage_field(usage, "input_tokens", "total_tokens", "prompt_tokens")
    output_tokens = _usage_field(usage, "output_tokens", "completion_tokens")
    cache_read = _usage_field(usage, "cache_read_input_tokens", "cache_read_tokens")
    cache_creation = _usage_field(
        usage, "cache_creation_input_tokens", "cache_creation_tokens"
    )

    if feature == AIUsageLog.FEATURE_EMBED:
        cost = embed_cost(model, input_tokens)
    else:
        cost = chat_cost(
            model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_creation_tokens=cache_creation,
        )

    AIUsageLog.objects.create(
        workspace_id=workspace_id,
        user_id=user_id,
        feature=feature,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_creation_tokens=cache_creation,
        cost_usd=cost,
    )
    return cost


def tokens_used_this_month(workspace_id) -> int:
    """Total ``input + output + cache_creation`` tokens for the
    current calendar month.

    Cache-read tokens deliberately don't count toward the budget: they
    are already a discount on fresh inputs, and double-counting them
    would punish the workspace for using prompt caching.
    """
    start = timezone.now().replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    agg = AIUsageLog.objects.filter(
        workspace_id=workspace_id, created_at__gte=start
    ).aggregate(
        i=Sum("input_tokens"),
        o=Sum("output_tokens"),
        c=Sum("cache_creation_tokens"),
    )
    return (agg["i"] or 0) + (agg["o"] or 0) + (agg["c"] or 0)


def budget_status(workspace_id) -> tuple[int, int, bool]:
    """Return ``(used, budget, exceeded)`` for a workspace.

    ``exceeded`` is True iff ``used >= budget`` (inclusive — at the
    line, the next request gets 429). If the workspace has no
    AI config, returns ``(0, 0, True)`` — caller treats that as a
    soft block.
    """
    cfg = (
        WorkspaceAIConfig.objects.filter(workspace_id=workspace_id, enabled=True)
        .only("monthly_token_budget")
        .first()
    )
    if cfg is None:
        return 0, 0, True
    used = tokens_used_this_month(workspace_id)
    return used, cfg.monthly_token_budget, used >= cfg.monthly_token_budget


# ---------------------------------------------------------------------------
# TZ 6.3 — read-side aggregations for the usage dashboard
# ---------------------------------------------------------------------------


def _billable_tokens_expr():
    """SQL expression for the "what counts toward the budget" sum.

    Matches the denominator in :func:`tokens_used_this_month`:
    ``input + output + cache_creation``. Cache-read is excluded — it's
    already a price discount on fresh input, double-counting would
    punish workspaces that use prompt caching.
    """
    return F("input_tokens") + F("output_tokens") + F("cache_creation_tokens")


# All five feature codes we expect to see. The dashboard pads its
# ``by_feature`` rollup so a feature with zero spend still appears
# (zero row vs missing row matters: it tells the admin "yes, this
# feature is tracked, just unused"). Keep this list in sync with
# ``AIUsageLog.FEATURE_CHOICES``.
ALL_FEATURES: tuple[str, ...] = (
    AIUsageLog.FEATURE_INTENT_SEARCH,
    AIUsageLog.FEATURE_SUMMARIZE,
    AIUsageLog.FEATURE_BULK,
    AIUsageLog.FEATURE_AGENT,
    AIUsageLog.FEATURE_EMBED,
)


def month_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    """Return ``[start_of_month, next_request_instant)`` as a default
    period for the dashboard. Inclusive start, exclusive end.

    Why not "month so far up to midnight tonight"? Because the budget
    is computed against THIS instant — showing the user a window that
    doesn't include the row that just landed two seconds ago would
    make the dashboard look broken right after a search.
    """
    now = now or timezone.now()
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start, now


def compute_usage_stats(
    workspace_id, *, start: datetime, end: datetime, top_users: int = 10
) -> dict:
    """Aggregate ``AIUsageLog`` for a workspace over ``[start, end)``.

    Returns a dict shaped::

        {
            "period": {"start": "...", "end": "..."},
            "totals": {
                "calls": int,
                "input_tokens": int, "output_tokens": int,
                "cache_read_tokens": int, "cache_creation_tokens": int,
                "billable_tokens": int,    # what counts against budget
                "cost_usd": "0.123456",    # Decimal serialised as str
            },
            "by_feature": [
                {"feature": "intent_search", "calls": int,
                 "billable_tokens": int, "cost_usd": "..."},
                ...   # all 5 features, padded with zeros
            ],
            "by_model": [
                {"model": "claude-sonnet-4-6", "calls": int,
                 "billable_tokens": int, "cost_usd": "..."},
                ...
            ],
            "by_user": [
                {"user_id": "<uuid|null>", "calls": int,
                 "billable_tokens": int, "cost_usd": "..."},
                ...   # top N by cost desc, capped at `top_users`
            ],
            "by_day": [
                {"date": "YYYY-MM-DD", "calls": int,
                 "billable_tokens": int, "cost_usd": "..."},
                ...   # every day in the window, including zero-spend
            ],
        }

    All sums are in-DB aggregates — one query per facet, no Python
    fan-in. ``ai_usage_ws_time_idx`` covers ``(workspace, created_at)``
    so the WHERE clause hits the index. Five small aggregate queries
    is well under the 300ms budget of the page even at 100k log rows
    per month.
    """
    base = AIUsageLog.objects.filter(
        workspace_id=workspace_id,
        created_at__gte=start,
        created_at__lt=end,
    )

    # ---- Totals (single aggregate) ---------------------------------
    totals = base.aggregate(
        calls=Count("id"),
        input_tokens=Sum("input_tokens"),
        output_tokens=Sum("output_tokens"),
        cache_read_tokens=Sum("cache_read_tokens"),
        cache_creation_tokens=Sum("cache_creation_tokens"),
        cost_usd=Sum("cost_usd"),
    )
    # Sum(...) returns None for an empty set — coerce to zero so the
    # frontend doesn't have to know about the Postgres NULL contract.
    for k in (
        "calls",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
    ):
        totals[k] = int(totals[k] or 0)
    totals["cost_usd"] = str(totals["cost_usd"] or Decimal("0"))
    totals["billable_tokens"] = (
        totals["input_tokens"]
        + totals["output_tokens"]
        + totals["cache_creation_tokens"]
    )

    # ---- by feature ------------------------------------------------
    by_feature_rows = list(
        base.values("feature")
        .annotate(
            calls=Count("id"),
            billable_tokens=Sum(_billable_tokens_expr()),
            cost_usd=Sum("cost_usd"),
        )
        .order_by("feature")
    )
    by_feature_map = {row["feature"]: row for row in by_feature_rows}
    by_feature = []
    for feature in ALL_FEATURES:
        row = by_feature_map.get(feature)
        if row is None:
            by_feature.append(
                {
                    "feature": feature,
                    "calls": 0,
                    "billable_tokens": 0,
                    "cost_usd": "0",
                }
            )
        else:
            by_feature.append(
                {
                    "feature": feature,
                    "calls": int(row["calls"]),
                    "billable_tokens": int(row["billable_tokens"] or 0),
                    "cost_usd": str(row["cost_usd"] or Decimal("0")),
                }
            )

    # ---- by model --------------------------------------------------
    by_model = [
        {
            "model": row["model"],
            "calls": int(row["calls"]),
            "billable_tokens": int(row["billable_tokens"] or 0),
            "cost_usd": str(row["cost_usd"] or Decimal("0")),
        }
        for row in base.values("model")
        .annotate(
            calls=Count("id"),
            billable_tokens=Sum(_billable_tokens_expr()),
            cost_usd=Sum("cost_usd"),
        )
        .order_by("-cost_usd")
    ]

    # ---- by user (top N) ------------------------------------------
    # NULL user happens when a SET_NULL ran (user deleted) — surface
    # that bucket too, so the totals reconcile. The UI labels it as
    # "(удалённый пользователь)".
    by_user = [
        {
            "user_id": str(row["user_id"]) if row["user_id"] else None,
            "calls": int(row["calls"]),
            "billable_tokens": int(row["billable_tokens"] or 0),
            "cost_usd": str(row["cost_usd"] or Decimal("0")),
        }
        for row in base.values("user_id")
        .annotate(
            calls=Count("id"),
            billable_tokens=Sum(_billable_tokens_expr()),
            cost_usd=Sum("cost_usd"),
        )
        .order_by("-cost_usd")[:top_users]
    ]

    # ---- by day (continuous trend) --------------------------------
    # ``TruncDate`` honours TIME_ZONE — Plane defaults to UTC. The
    # dashboard renders the timestamp in the user's local tz; we
    # surface the date label as ISO ``YYYY-MM-DD``.
    by_day_raw = {
        row["d"].isoformat(): row
        for row in base.annotate(d=TruncDate("created_at"))
        .values("d")
        .annotate(
            calls=Count("id"),
            billable_tokens=Sum(_billable_tokens_expr()),
            cost_usd=Sum("cost_usd"),
        )
    }
    # Fill in zero-spend days so the chart line doesn't skip dates.
    by_day = []
    # `end` is exclusive (compute_usage_stats period contract), so the
    # number of full days included is `(end - start).days`. Add 1 only
    # when end is later than start by some intra-day amount but the
    # date hasn't rolled over yet — we always want at least one row.
    span_days = max(int((end.date() - start.date()).days), 1)
    # Cap to one year (366 days) to keep the response bounded even on
    # an absurd ?from/to. The endpoint validates this server-side too.
    span_days = min(span_days, 366)
    for i in range(span_days):
        date = start.date() + timedelta(days=i)
        key = date.isoformat()
        row = by_day_raw.get(key)
        if row is None:
            by_day.append(
                {
                    "date": key,
                    "calls": 0,
                    "billable_tokens": 0,
                    "cost_usd": "0",
                }
            )
        else:
            by_day.append(
                {
                    "date": key,
                    "calls": int(row["calls"]),
                    "billable_tokens": int(row["billable_tokens"] or 0),
                    "cost_usd": str(row["cost_usd"] or Decimal("0")),
                }
            )

    return {
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "totals": totals,
        "by_feature": by_feature,
        "by_model": by_model,
        "by_user": by_user,
        "by_day": by_day,
    }
