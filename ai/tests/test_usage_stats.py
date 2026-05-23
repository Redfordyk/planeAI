"""TZ 6.3 — usage dashboard aggregation endpoint.

Three concerns under test:

  1. ACL — admin only. A workspace member with role=MEMBER cannot
     see cost data; cross-workspace access is impossible.
  2. Numbers match ``AIUsageLog``. Totals, per-feature, per-user and
     per-day rollups all reconcile against the raw rows the fixture
     plants.
  3. Period contract — defaults, explicit ranges, malformed input
     handling, 366-day cap.

We don't go through HTTP for the pure aggregation tests — using
``compute_usage_stats`` directly keeps them fast and decoupled from
DRF / URL routing — but the ACL + 400 paths *do* go through the
view because that's the contract the frontend depends on.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from ai.models import AIUsageLog
from ai.usage import ALL_FEATURES, compute_usage_stats, month_window


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _record(
    *,
    workspace,
    user,
    feature: str,
    model: str = "claude-sonnet-4-6",
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    cost_usd: str = "0",
    created_at: datetime | None = None,
) -> AIUsageLog:
    row = AIUsageLog.objects.create(
        workspace=workspace,
        user=user,
        feature=feature,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_creation_tokens=cache_creation_tokens,
        cost_usd=Decimal(cost_usd),
    )
    if created_at is not None:
        # auto_now_add stamps created_at — override for trend tests.
        AIUsageLog.objects.filter(pk=row.pk).update(created_at=created_at)
        row.refresh_from_db()
    return row


def _auth(client: APIClient, user) -> None:
    client.force_authenticate(user=user)


# ---------------------------------------------------------------------------
# compute_usage_stats — pure aggregation
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_totals_match_raw_rows(make_user, make_workspace):
    """Sum across every column matches the rows we planted. The
    invariant "dashboard numbers == AIUsageLog numbers" is the
    central correctness claim of TZ 6.3."""
    ws = make_workspace(owner=make_user("owner"))
    u = make_user("u")
    _record(
        workspace=ws,
        user=u,
        feature=AIUsageLog.FEATURE_INTENT_SEARCH,
        input_tokens=1000,
        output_tokens=500,
        cache_creation_tokens=200,
        cache_read_tokens=300,
        cost_usd="0.123456",
    )
    _record(
        workspace=ws,
        user=u,
        feature=AIUsageLog.FEATURE_AGENT,
        input_tokens=400,
        output_tokens=100,
        cost_usd="0.010000",
    )
    start, end = month_window(timezone.now())
    stats = compute_usage_stats(ws.id, start=start, end=end)

    assert stats["totals"]["calls"] == 2
    assert stats["totals"]["input_tokens"] == 1400
    assert stats["totals"]["output_tokens"] == 600
    assert stats["totals"]["cache_creation_tokens"] == 200
    assert stats["totals"]["cache_read_tokens"] == 300
    # Billable = input + output + cache_creation. Cache READ excluded.
    assert stats["totals"]["billable_tokens"] == 1400 + 600 + 200
    assert Decimal(stats["totals"]["cost_usd"]) == Decimal("0.133456")


@pytest.mark.django_db
def test_by_feature_pads_all_five_features(make_user, make_workspace):
    """A feature with zero spend still appears as a row — the chart
    legend mustn't lose it just because nobody used it this month."""
    ws = make_workspace(owner=make_user("owner"))
    u = make_user("u")
    _record(
        workspace=ws,
        user=u,
        feature=AIUsageLog.FEATURE_INTENT_SEARCH,
        input_tokens=10,
        cost_usd="0.001",
    )
    start, end = month_window(timezone.now())
    stats = compute_usage_stats(ws.id, start=start, end=end)
    features = {row["feature"] for row in stats["by_feature"]}
    assert features == set(ALL_FEATURES)
    # Search row has the right numbers, the rest are zeroed.
    for row in stats["by_feature"]:
        if row["feature"] == AIUsageLog.FEATURE_INTENT_SEARCH:
            assert row["calls"] == 1
            assert row["billable_tokens"] == 10
            assert Decimal(row["cost_usd"]) == Decimal("0.001")
        else:
            assert row["calls"] == 0
            assert row["billable_tokens"] == 0
            assert Decimal(row["cost_usd"]) == Decimal("0")


@pytest.mark.django_db
def test_by_user_sorted_descending_and_capped(make_user, make_workspace):
    """Top users come back ordered by cost descending and capped at
    ``top_users``. The dashboard renders a top-10 leaderboard."""
    ws = make_workspace(owner=make_user("owner"))
    users = [make_user(f"u{i}") for i in range(12)]
    for i, u in enumerate(users):
        # Increasing cost — last user is the most expensive.
        _record(
            workspace=ws,
            user=u,
            feature=AIUsageLog.FEATURE_INTENT_SEARCH,
            input_tokens=10 * (i + 1),
            cost_usd=f"0.{i + 1:03d}",
        )
    start, end = month_window(timezone.now())
    stats = compute_usage_stats(ws.id, start=start, end=end, top_users=5)
    assert len(stats["by_user"]) == 5
    costs = [Decimal(r["cost_usd"]) for r in stats["by_user"]]
    assert costs == sorted(costs, reverse=True)
    # The top entry is user u11 with cost 0.012.
    assert Decimal(stats["by_user"][0]["cost_usd"]) == Decimal("0.012")


@pytest.mark.django_db
def test_by_day_fills_zero_spend_days(make_user, make_workspace):
    """Every day in the window is present, even days with no calls.
    Otherwise Recharts draws a broken-looking line."""
    ws = make_workspace(owner=make_user("owner"))
    u = make_user("u")
    now = datetime(2026, 5, 15, 12, 0, tzinfo=dt_timezone.utc)
    # One row on day 0, one row on day 4 — days 1, 2, 3 must appear
    # with zero spend.
    _record(workspace=ws, user=u, feature=AIUsageLog.FEATURE_AGENT,
            input_tokens=10, cost_usd="0.001", created_at=now)
    _record(workspace=ws, user=u, feature=AIUsageLog.FEATURE_AGENT,
            input_tokens=20, cost_usd="0.002",
            created_at=now + timedelta(days=4))
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=5)  # 5-day window
    stats = compute_usage_stats(ws.id, start=start, end=end)
    assert [r["date"] for r in stats["by_day"]] == [
        "2026-05-15", "2026-05-16", "2026-05-17", "2026-05-18", "2026-05-19",
    ]
    # Day 0 and day 4 have spend, the middle three are zero.
    assert stats["by_day"][0]["calls"] == 1
    assert stats["by_day"][1]["calls"] == 0
    assert stats["by_day"][4]["calls"] == 1


@pytest.mark.django_db
def test_workspace_isolation(make_user, make_workspace):
    """A row in workspace A never bleeds into workspace B's stats —
    the CLAUDE.md invariant ('изоляция воркспейсов') applies here
    just as much as to retrieval."""
    ws_a = make_workspace(slug="a", owner=make_user("a-owner"))
    ws_b = make_workspace(slug="b", owner=make_user("b-owner"))
    u = make_user("u")
    _record(
        workspace=ws_a,
        user=u,
        feature=AIUsageLog.FEATURE_AGENT,
        input_tokens=999,
        cost_usd="9.99",
    )
    start, end = month_window(timezone.now())
    stats_b = compute_usage_stats(ws_b.id, start=start, end=end)
    assert stats_b["totals"]["calls"] == 0
    assert stats_b["totals"]["input_tokens"] == 0
    assert Decimal(stats_b["totals"]["cost_usd"]) == Decimal("0")


@pytest.mark.django_db
def test_period_excludes_rows_outside_window(make_user, make_workspace):
    """Only rows inside ``[start, end)`` count. Important for the
    "last 7 days" presets and historical month navigation."""
    ws = make_workspace(owner=make_user("owner"))
    u = make_user("u")
    now = datetime(2026, 5, 15, 12, 0, tzinfo=dt_timezone.utc)
    _record(
        workspace=ws, user=u, feature=AIUsageLog.FEATURE_INTENT_SEARCH,
        input_tokens=10, cost_usd="0.001",
        created_at=now - timedelta(days=10),  # OUTSIDE window
    )
    _record(
        workspace=ws, user=u, feature=AIUsageLog.FEATURE_INTENT_SEARCH,
        input_tokens=20, cost_usd="0.002",
        created_at=now,  # INSIDE
    )
    start = now - timedelta(days=3)
    end = now + timedelta(days=1)
    stats = compute_usage_stats(ws.id, start=start, end=end)
    assert stats["totals"]["calls"] == 1
    assert stats["totals"]["input_tokens"] == 20


# ---------------------------------------------------------------------------
# UsageStatsView — HTTP layer
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_view_403_for_non_admin(
    make_user, make_workspace, make_workspace_member
):
    """A workspace MEMBER (role=15) is not allowed — costs are
    admin-grade information."""
    owner = make_user("owner")
    ws = make_workspace(owner=owner)
    member = make_user("member")
    make_workspace_member(workspace=ws, user=member, role=15)

    client = APIClient()
    _auth(client, member)
    resp = client.get(f"/api/ai/workspaces/{ws.id}/usage/stats/")
    assert resp.status_code == 403


@pytest.mark.django_db
def test_view_403_for_outsider(make_user, make_workspace):
    """Someone with no membership row at all."""
    ws = make_workspace(owner=make_user("owner"))
    outsider = make_user("outsider")
    client = APIClient()
    _auth(client, outsider)
    resp = client.get(f"/api/ai/workspaces/{ws.id}/usage/stats/")
    assert resp.status_code == 403


@pytest.mark.django_db
def test_view_200_for_admin_returns_full_shape(
    make_user, make_workspace, make_workspace_member, make_ai_config
):
    """Happy path: admin gets the full payload, every documented top-
    level key is present, and the budget panel is filled in."""
    owner = make_user("owner")
    ws = make_workspace(owner=owner)
    make_workspace_member(workspace=ws, user=owner, role=20)
    make_ai_config(ws, monthly_token_budget=1_000_000)
    _record(
        workspace=ws,
        user=owner,
        feature=AIUsageLog.FEATURE_INTENT_SEARCH,
        input_tokens=100,
        output_tokens=50,
        cost_usd="0.01",
    )

    client = APIClient()
    _auth(client, owner)
    resp = client.get(f"/api/ai/workspaces/{ws.id}/usage/stats/")
    assert resp.status_code == 200
    body = resp.json()
    for key in ("period", "totals", "by_feature", "by_model", "by_user", "by_day", "budget"):
        assert key in body
    assert body["budget"]["tokens_budget"] == 1_000_000
    assert body["budget"]["tokens_used"] == 100 + 50  # input + output
    assert body["budget"]["level"] == "ok"


@pytest.mark.django_db
def test_view_400_on_malformed_dates(
    make_user, make_workspace, make_workspace_member
):
    owner = make_user("owner")
    ws = make_workspace(owner=owner)
    make_workspace_member(workspace=ws, user=owner, role=20)

    client = APIClient()
    _auth(client, owner)
    resp = client.get(
        f"/api/ai/workspaces/{ws.id}/usage/stats/",
        {"from": "garbage", "to": "garbage"},
    )
    assert resp.status_code == 400


@pytest.mark.django_db
def test_view_400_when_only_one_of_from_to(
    make_user, make_workspace, make_workspace_member
):
    """Passing only ``from`` is ambiguous (what's ``to``?) — 400."""
    owner = make_user("owner")
    ws = make_workspace(owner=owner)
    make_workspace_member(workspace=ws, user=owner, role=20)

    client = APIClient()
    _auth(client, owner)
    resp = client.get(
        f"/api/ai/workspaces/{ws.id}/usage/stats/",
        {"from": "2026-05-01T00:00:00Z"},
    )
    assert resp.status_code == 400


@pytest.mark.django_db
def test_view_400_on_period_over_max(
    make_user, make_workspace, make_workspace_member
):
    """The 366-day cap protects against a runaway query plan."""
    owner = make_user("owner")
    ws = make_workspace(owner=owner)
    make_workspace_member(workspace=ws, user=owner, role=20)

    client = APIClient()
    _auth(client, owner)
    resp = client.get(
        f"/api/ai/workspaces/{ws.id}/usage/stats/",
        {"from": "2024-01-01T00:00:00Z", "to": "2026-01-01T00:00:00Z"},
    )
    assert resp.status_code == 400
    assert b"366" in resp.content


@pytest.mark.django_db
def test_view_budget_level_warning_at_80pct(
    make_user, make_workspace, make_workspace_member, make_ai_config
):
    """Budget panel transitions through ok → warning → critical →
    exceeded as the ratio climbs — same thresholds as the Prometheus
    rules in TZ 6.2."""
    owner = make_user("owner")
    ws = make_workspace(owner=owner)
    make_workspace_member(workspace=ws, user=owner, role=20)
    make_ai_config(ws, monthly_token_budget=1_000)
    # Push ratio to 0.85.
    _record(
        workspace=ws, user=owner, feature=AIUsageLog.FEATURE_AGENT,
        input_tokens=850, cost_usd="0",
    )
    client = APIClient()
    _auth(client, owner)
    resp = client.get(f"/api/ai/workspaces/{ws.id}/usage/stats/")
    assert resp.status_code == 200
    assert resp.json()["budget"]["level"] == "warning"


@pytest.mark.django_db
def test_view_budget_level_exceeded(
    make_user, make_workspace, make_workspace_member, make_ai_config
):
    owner = make_user("owner")
    ws = make_workspace(owner=owner)
    make_workspace_member(workspace=ws, user=owner, role=20)
    make_ai_config(ws, monthly_token_budget=100)
    _record(
        workspace=ws, user=owner, feature=AIUsageLog.FEATURE_AGENT,
        input_tokens=200, cost_usd="0",
    )
    client = APIClient()
    _auth(client, owner)
    resp = client.get(f"/api/ai/workspaces/{ws.id}/usage/stats/")
    body = resp.json()
    assert body["budget"]["exceeded"] is True
    assert body["budget"]["level"] == "exceeded"
