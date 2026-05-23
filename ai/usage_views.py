"""TZ 6.3 — workspace AI usage dashboard endpoints.

A single read-only endpoint:

  ``GET /api/ai/workspaces/<workspace_id>/usage/stats/?from=...&to=...``

Returns aggregated token / dollar spend grouped by feature, model and
user, plus a daily trend and total-vs-budget rollup. The frontend
``UsageDashboard`` (apps/web/core/components/ai/usage-dashboard.tsx)
binds 1:1 to this shape.

ACL: workspace **admin** only. Token usage exposes cost-per-user
which is a finance-grade signal — Plane's standard "any member can
see settings" rule doesn't apply. Mirrors the existing admin gate
used by :class:`AgentDetailView`.

We deliberately keep the budget reading separate from the cost
aggregation: ``budget_status`` uses the live "this calendar month"
window (matches the guard in ``ai.guards``), while the aggregation
honours the caller-supplied ``[from, to)``. If the caller asks for a
historical month, the budget panel still reflects today's budget
state — that's the correct contract because the budget cap is a
*current* constraint.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone as dt_timezone
from typing import Any

from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from ai.agent_views import _is_workspace_admin
from ai.usage import budget_status, compute_usage_stats, month_window


logger = logging.getLogger("plane.ai.usage_views")


# Hard cap on the period the endpoint will aggregate. 366 days covers
# "last year" requests; anything wider is almost certainly a typo'd
# epoch and would scan a huge chunk of ``ai_usage_log``. The frontend
# already restricts the date pickers but the server still enforces.
MAX_PERIOD_DAYS = 366


def _parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 timestamp tolerating the ``Z`` suffix the
    JS ``toISOString()`` emits. Returns an aware datetime in UTC."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    return parsed


class UsageStatsView(APIView):
    """``GET /api/ai/workspaces/<workspace_id>/usage/stats/``.

    Query parameters (all optional, mutually consistent):

      - ``from`` (ISO-8601) — period start, inclusive
      - ``to`` (ISO-8601) — period end, exclusive
      - ``top_users`` (int, default 10, cap 50) — how many users to
        return in ``by_user``

    Without ``from`` / ``to`` the period defaults to "this month so
    far" — the same window the budget guard cares about.

    Errors:
      - 400 — malformed ISO-8601, ``from > to``, or period > 366 days
      - 403 — caller is not a workspace admin
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, workspace_id) -> Response:
        if not _is_workspace_admin(request.user, workspace_id):
            return Response(
                {"error": "workspace admin required"},
                status=status.HTTP_403_FORBIDDEN,
            )

        start, end = self._resolve_period(request)
        if isinstance(start, Response):  # bubbled validation error
            return start

        try:
            top_users = int(request.query_params.get("top_users") or 10)
        except (TypeError, ValueError):
            return Response(
                {"error": "'top_users' must be an integer"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        top_users = max(1, min(top_users, 50))

        stats = compute_usage_stats(
            workspace_id, start=start, end=end, top_users=top_users
        )

        used, budget, exceeded = budget_status(workspace_id)
        stats["budget"] = self._budget_panel(used, budget, exceeded)
        return Response(stats)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _resolve_period(self, request) -> tuple[datetime, datetime] | Response:
        """Return ``(start, end)`` or a 400 response.

        - Both unset → current calendar month so far.
        - Only one set → 400 (would be ambiguous).
        - Both set → parsed and validated.
        """
        raw_from = request.query_params.get("from")
        raw_to = request.query_params.get("to")
        if not raw_from and not raw_to:
            return month_window(timezone.now())
        if bool(raw_from) ^ bool(raw_to):
            return Response(
                {"error": "'from' and 'to' must both be provided or both omitted"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            start = _parse_iso(raw_from)
            end = _parse_iso(raw_to)
        except ValueError:
            return Response(
                {"error": "invalid ISO-8601 in 'from' or 'to'"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if start >= end:
            return Response(
                {"error": "'from' must be strictly before 'to'"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if (end - start).days > MAX_PERIOD_DAYS:
            return Response(
                {"error": f"period must be ≤ {MAX_PERIOD_DAYS} days"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return start, end

    @staticmethod
    def _budget_panel(used: int, budget: int, exceeded: bool) -> dict[str, Any]:
        """Shape the budget header card.

        ``ratio`` is in [0, +inf): after the hard-cap fires, the
        last successful call may have pushed the counter slightly
        over 1.0, which is informative — the UI clamps the bar to
        100% but shows the raw percentage as text.
        """
        ratio = (used / budget) if budget else 0.0
        if budget == 0:
            level = "unset"  # workspace has no AI config / disabled
        elif exceeded:
            level = "exceeded"
        elif ratio >= 0.95:
            level = "critical"
        elif ratio >= 0.80:
            level = "warning"
        else:
            level = "ok"
        return {
            "tokens_used": used,
            "tokens_budget": budget,
            "ratio": round(ratio, 4),
            "exceeded": exceeded,
            "level": level,
        }


__all__ = ["UsageStatsView", "MAX_PERIOD_DAYS"]
