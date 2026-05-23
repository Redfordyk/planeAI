"""TZ 6.8 — rollout status snapshot.

Two concerns under test:

  1. **Per-workspace aggregation** matches what the PM expects to
     see: calls/tokens/cost in the rolling window, applied/rejected
     agent counts, last action snapshot, month-to-date totals.

  2. **Operator notes** fire when the metric crosses a threshold —
     budget warning, agent-rejection skew, silent workspace. These
     notes are the "what to look at first" hints the PM acts on,
     so a regression here would let an unhealthy workspace slip
     through the daily sweep.
"""

from __future__ import annotations

import io
import json
from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.management import call_command
from django.utils import timezone

from ai.models import (
    AIAgent,
    AIAgentActionLog,
    AIUsageLog,
)


def _run_json(**kw) -> dict:
    out = io.StringIO()
    call_command("rollout_status", stdout=out, stderr=io.StringIO(), as_json=True, **kw)
    return json.loads(out.getvalue().strip().splitlines()[-1])


def _record(workspace, user, *, cost="0.01", input_tokens=100, hours_ago=1):
    """Insert one AIUsageLog row with controllable timestamp."""
    row = AIUsageLog.objects.create(
        workspace=workspace,
        user=user,
        feature=AIUsageLog.FEATURE_INTENT_SEARCH,
        model="claude-sonnet-4-6",
        input_tokens=input_tokens,
        cost_usd=Decimal(cost),
    )
    if hours_ago:
        AIUsageLog.objects.filter(pk=row.pk).update(
            created_at=timezone.now() - timedelta(hours=hours_ago)
        )
    return row


# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_snapshot_shape_for_one_workspace(
    make_user, make_workspace, make_ai_config
):
    owner = make_user("owner")
    ws = make_workspace(owner=owner, slug="alpha")
    make_ai_config(ws, monthly_token_budget=10_000)
    _record(ws, owner, cost="0.05", input_tokens=200, hours_ago=1)

    body = _run_json(workspace="alpha", since=24)
    assert len(body["workspaces"]) == 1
    ws_status = body["workspaces"][0]
    assert ws_status["workspace_slug"] == "alpha"
    assert ws_status["enabled"] is True
    assert ws_status["calls_24h"] == 1
    assert ws_status["billable_tokens_24h"] == 200
    assert Decimal(ws_status["cost_usd_24h"]) == Decimal("0.05")


@pytest.mark.django_db
def test_excludes_calls_outside_window(
    make_user, make_workspace, make_ai_config
):
    """Rows older than ``--since`` hours don't count toward the
    rolling window — they DO count toward month-to-date."""
    owner = make_user("owner")
    ws = make_workspace(owner=owner)
    make_ai_config(ws)
    # In-window
    _record(ws, owner, cost="0.01", hours_ago=1)
    # Out-of-window (last week)
    _record(ws, owner, cost="0.99", hours_ago=168)

    body = _run_json(workspace=str(ws.id), since=24)
    ws_status = body["workspaces"][0]
    assert ws_status["calls_24h"] == 1
    assert Decimal(ws_status["cost_usd_24h"]) == Decimal("0.01")
    # Month-to-date includes both rows (assuming the run is in the
    # same calendar month as 168h ago).
    assert Decimal(ws_status["cost_usd_month"]) >= Decimal("0.01")


@pytest.mark.django_db
def test_kill_switch_surfaced_as_note(
    make_user, make_workspace, make_ai_config
):
    """A disabled workspace is in the listing but flagged in notes —
    so the PM doesn't quietly miss that AI is off."""
    ws = make_workspace(owner=make_user("o"))
    make_ai_config(ws, enabled=False)
    body = _run_json(workspace=str(ws.id))
    ws_status = body["workspaces"][0]
    assert ws_status["enabled"] is False
    assert any("DISABLED" in n for n in ws_status["notes"])


@pytest.mark.django_db
def test_budget_warning_note_at_80pct(
    make_user, make_workspace, make_ai_config
):
    """Ratio ≥ 0.80 triggers a budget note. Mirrors the Prometheus
    threshold (TZ 6.2 PlaneAIBudgetWarning)."""
    owner = make_user("owner")
    ws = make_workspace(owner=owner)
    make_ai_config(ws, monthly_token_budget=1_000)
    # Push ratio to 0.85 — input alone counts as billable.
    _record(ws, owner, input_tokens=850, hours_ago=0)

    body = _run_json(workspace=str(ws.id))
    notes = body["workspaces"][0]["notes"]
    assert any("budget" in n.lower() for n in notes)


@pytest.mark.django_db
def test_agent_reject_skew_note(
    make_user, make_workspace, make_workspace_member, make_ai_config,
    make_project,
):
    """When the agent rejects more than it applies (and rejections
    are > 5), the PM gets a note — that's the "model is wandering
    off the white-list" signature."""
    owner = make_user("owner")
    ws = make_workspace(owner=owner)
    make_workspace_member(workspace=ws, user=owner, role=20)
    make_ai_config(ws)
    project = make_project(workspace=ws, created_by=owner)
    agent_user = make_user("bot")
    make_workspace_member(workspace=ws, user=agent_user, role=15)
    agent = AIAgent.objects.create(user=agent_user, workspace=ws, enabled=True)

    # 7 rejected, 1 applied.
    for i in range(7):
        AIAgentActionLog.objects.create(
            agent=agent,
            workspace=ws,
            project=project,
            issue_id="00000000-0000-0000-0000-00000000000{}".format(i % 10),
            tool_name="set_priority",
            input={"priority": "high"},
            status=AIAgentActionLog.STATUS_REJECTED,
            error="",
        )
    AIAgentActionLog.objects.create(
        agent=agent,
        workspace=ws,
        project=project,
        issue_id="11111111-1111-1111-1111-111111111111",
        tool_name="set_priority",
        input={"priority": "low"},
        status=AIAgentActionLog.STATUS_APPLIED,
        error="",
    )

    body = _run_json(workspace=str(ws.id))
    notes = body["workspaces"][0]["notes"]
    assert any("rejected" in n.lower() for n in notes)
    # The counts surface raw so the PM can also build their own
    # judgement from JSON.
    counts = body["workspaces"][0]["agent_actions_24h"]
    assert counts["rejected"] == 7
    assert counts["applied"] == 1


@pytest.mark.django_db
def test_silent_workspace_note(make_user, make_workspace, make_ai_config):
    """No AI calls in 24h on an enabled workspace = the PM should
    see "no AI calls" as a yellow flag (something might be broken
    in the ingest path; nobody is using it; or kill switch was
    flipped externally). The note is informative, not alarming —
    the PM judges from context."""
    ws = make_workspace(owner=make_user("o"))
    make_ai_config(ws)
    body = _run_json(workspace=str(ws.id))
    notes = body["workspaces"][0]["notes"]
    assert any("no AI calls" in n for n in notes)


@pytest.mark.django_db
def test_lists_all_workspaces_without_filter(
    make_user, make_workspace, make_ai_config
):
    """Without ``--workspace``, the command snapshots every
    WorkspaceAIConfig row. That's the daily "all rollout cohorts at
    once" view."""
    ws_a = make_workspace(slug="cohort1", owner=make_user("a"))
    ws_b = make_workspace(slug="cohort2", owner=make_user("b"))
    make_ai_config(ws_a)
    make_ai_config(ws_b)

    body = _run_json()
    slugs = {w["workspace_slug"] for w in body["workspaces"]}
    assert slugs == {"cohort1", "cohort2"}
