"""Tests for the multi-agent orchestrator (phases 7-12).

LLM calls are mocked. We verify:

  - Decision Layer matrix returns expected risk levels.
  - Forbidden actions (delete_*) return None.
  - Circuit breaker opens when count >= cap.
  - Kill switch blocks router.
  - log_action writes AgentAction with correct status mapping.
  - PLANNER validation caps at 60 tasks, drops invalid items.
  - MONITOR detects blocker label, writes PredictedRisk with unique-when-open
    constraint (re-scan updates not duplicates).
  - EXECUTOR picks lowest-load member.
  - Router with kill switch records rejection.
  - Router drops modified_by_agent events.
  - Goals REST endpoint creates + lists.
  - Activity feed REST endpoint returns actions.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
from rest_framework.test import APIClient

from ai.models import AgentAction, PredictedRisk, ProjectGoal, TeamVelocity
from ai.orchestrator import decision, breaker, base
from ai.orchestrator.events import Event, ISSUE_UPDATED, ISSUE_CREATED


# ---- Decision Layer -----------------------------------------------------


def test_decision_matrix_returns_expected_levels():
    assert decision.decide("set_label") == "AUTO"
    assert decision.decide("reassign_task") == "CONFIRM"
    assert decision.decide("hire_freelancer") == "ESCALATE"
    assert decision.decide("delete_issue") is None
    assert decision.decide("totally_unknown") == "CONFIRM"  # safe default


def test_decision_critical_path_escalates_one_notch():
    assert decision.decide("set_label", on_critical_path=True) == "NOTIFY"
    assert decision.decide("reassign_task", on_critical_path=True) == "ESCALATE"
    assert decision.decide("delete_issue", on_critical_path=True) is None


def test_is_forbidden_returns_true_for_delete():
    assert decision.is_forbidden("delete_issue")
    assert not decision.is_forbidden("set_label")


# ---- log_action ---------------------------------------------------------


def test_log_action_maps_status_from_risk(db, make_workspace, make_ai_config):
    ws = make_workspace()
    make_ai_config(ws)
    a = base.log_action(
        workspace_id=ws.id, agent_type="PLANNER", action_type="set_label",
        reasoning="r",
    )
    assert a.status == "applied"
    assert a.risk_level == "AUTO"
    b = base.log_action(
        workspace_id=ws.id, agent_type="PLANNER", action_type="reassign_task",
        reasoning="r",
    )
    assert b.status == "proposed"
    assert b.risk_level == "CONFIRM"


def test_log_action_rejects_forbidden(db, make_workspace, make_ai_config):
    ws = make_workspace()
    make_ai_config(ws)
    a = base.log_action(
        workspace_id=ws.id, agent_type="PLANNER", action_type="delete_issue",
    )
    assert a.status == "rejected"


# ---- Breaker + kill switch ---------------------------------------------


def test_kill_switch_blocks(db, make_workspace, make_ai_config):
    ws = make_workspace()
    cfg = make_ai_config(ws)
    cfg.agents_killed = True
    cfg.save()
    with pytest.raises(breaker.AgentsHalted):
        breaker.ensure_agents_allowed(ws.id)


def test_breaker_opens_at_cap(db, make_workspace, make_ai_config):
    ws = make_workspace()
    cfg = make_ai_config(ws)
    cfg.max_agent_actions_per_hour = 3
    cfg.save()
    # write 3 applied actions
    for _ in range(3):
        AgentAction.objects.create(
            workspace_id=ws.id,
            agent_type="PLANNER", action_type="set_label",
            risk_level="AUTO", status="applied",
        )
    with pytest.raises(breaker.AgentsHalted) as exc:
        breaker.ensure_agents_allowed(ws.id)
    assert "circuit_breaker_open" in str(exc.value)


# ---- PLANNER validation -------------------------------------------------


def test_planner_validate_caps_at_60_tasks():
    from ai.orchestrator.planner import _validate_plan
    huge_raw = {
        "epics": [
            {"name": "Big epic", "rationale": "go", "tasks": [
                {"name": f"task {i}", "description": "", "priority": "medium",
                 "estimated_hours": 4}
                for i in range(150)
            ]}
        ],
        "summary": "x", "critical_path": [],
    }
    plan = _validate_plan(huge_raw)
    assert plan["task_count"] == 60
    assert len(plan["epics"][0]["tasks"]) == 60


def test_planner_validate_drops_empty_and_clips_strings():
    from ai.orchestrator.planner import _validate_plan
    plan = _validate_plan({
        "epics": [
            {"name": "E", "tasks": [
                {"name": "", "description": ""},   # empty name -> dropped
                {"name": "ok", "priority": "weird", "estimated_hours": 1e9},
                {"name": "x" * 500, "description": "y" * 9000},  # long
            ]}
        ],
        "summary": "z", "critical_path": [],
    })
    tasks = plan["epics"][0]["tasks"]
    assert len(tasks) == 2
    assert tasks[0]["priority"] == "medium"  # invalid -> default
    assert tasks[0]["estimated_hours"] is None  # out-of-range -> None
    assert len(tasks[1]["name"]) <= 200
    assert len(tasks[1]["description"]) <= 2000


# ---- MONITOR ------------------------------------------------------------


def test_monitor_detects_blocker_label_and_upserts_risk(
    db, make_workspace, make_ai_config, make_project, make_issue, make_workspace_member, make_user
):
    from plane.db.models import Label
    from ai.orchestrator import monitor

    owner = make_user("owner")
    ws = make_workspace(owner=owner)
    make_workspace_member(workspace=ws, user=owner, role=20)
    cfg = make_ai_config(ws)
    cfg.max_agent_actions_per_hour = 1000
    cfg.save()
    prj = make_project(workspace=ws, created_by=owner)
    issue = make_issue(workspace=ws, project=prj, name="blocked task")
    label = Label.objects.create(
        name="blocked", workspace=ws, project=prj, color="#f00"
    )
    from plane.db.models import IssueLabel
    IssueLabel.objects.create(
        issue=issue, label=label, workspace=ws, project=prj
    )

    result = monitor.scan_project(ws.id, prj.id)
    assert result["risks"] >= 1
    risks = PredictedRisk.objects.filter(issue_id=issue.id, risk_type="blocker")
    assert risks.count() == 1
    assert risks.first().confidence >= 0.9

    # Rescan: should UPDATE not duplicate.
    monitor.scan_project(ws.id, prj.id)
    assert PredictedRisk.objects.filter(
        issue_id=issue.id, risk_type="blocker", resolved=False
    ).count() == 1


# ---- EXECUTOR -----------------------------------------------------------


def test_executor_picks_lowest_load_member(
    db, make_workspace, make_ai_config, make_project, make_issue, make_user, make_workspace_member
):
    from plane.db.models import ProjectMember, IssueAssignee
    from ai.orchestrator import executor

    owner = make_user("owner")
    busy = make_user("busy")
    free = make_user("free")
    ws = make_workspace(owner=owner)
    for u in (owner, busy, free):
        make_workspace_member(workspace=ws, user=u, role=20)
    cfg = make_ai_config(ws)
    cfg.max_agent_actions_per_hour = 1000
    cfg.save()
    prj = make_project(workspace=ws, created_by=owner)
    for u in (owner, busy, free):
        ProjectMember.objects.create(workspace=ws, project=prj, member=u, role=15)
    # Make busy carry 5 open issues
    for _ in range(5):
        i = make_issue(workspace=ws, project=prj, name="other")
        IssueAssignee.objects.create(workspace=ws, project=prj, issue=i, assignee=busy)
    target = make_issue(workspace=ws, project=prj, name="needs assignee")

    out = executor.suggest_assignee_for(target.id)
    assert out["suggested_user_id"] in (str(free.id), str(owner.id))
    assert out["load"] == 0


# ---- Router -------------------------------------------------------------


def test_router_drops_modified_by_agent(db, make_workspace, make_ai_config):
    from ai.orchestrator import router
    ws = make_workspace()
    make_ai_config(ws)
    ev = Event(type=ISSUE_UPDATED, workspace_id=str(ws.id), modified_by_agent=True).to_dict()
    out = router.handle_event(ev)
    assert out["skipped"] == "modified_by_agent"


def test_router_records_kill_switch_rejection(db, make_workspace, make_ai_config, make_project, make_user):
    from ai.orchestrator import router, breaker as br
    owner = make_user("owner")
    ws = make_workspace(owner=owner)
    cfg = make_ai_config(ws)
    prj = make_project(workspace=ws, created_by=owner)
    br.engage_kill_switch(ws.id, reason="test")
    ev = Event(
        type=ISSUE_CREATED,
        workspace_id=str(ws.id),
        project_id=str(prj.id),
        issue_id=str(uuid.uuid4()),
    ).to_dict()
    out = router.handle_event(ev)
    assert "halted" in out["skipped"]
    # Rejection AgentAction recorded
    assert AgentAction.objects.filter(workspace_id=ws.id, status="rejected").exists()


# ---- REST endpoints -----------------------------------------------------


def test_goal_list_create_endpoint(
    db, client, make_workspace, make_ai_config, make_user, make_workspace_member
):
    owner = make_user("owner")
    ws = make_workspace(owner=owner)
    make_workspace_member(workspace=ws, user=owner, role=20)
    make_ai_config(ws)
    c = APIClient()
    c.force_authenticate(owner)
    # Create goal without running planner (avoids LLM call)
    resp = c.post(
        f"/api/ai/workspaces/{ws.id}/orchestrator/goals/",
        {"title": "Ship MVP", "deadline": "2026-12-31", "run_planner": False},
        format="json",
    )
    assert resp.status_code == 201
    assert resp.data["goal"]["title"] == "Ship MVP"

    resp = c.get(f"/api/ai/workspaces/{ws.id}/orchestrator/goals/")
    assert resp.status_code == 200
    assert len(resp.data["goals"]) == 1


def test_activity_feed_endpoint(db, make_workspace, make_ai_config, make_user, make_workspace_member):
    owner = make_user("owner")
    ws = make_workspace(owner=owner)
    make_workspace_member(workspace=ws, user=owner, role=20)
    make_ai_config(ws)
    AgentAction.objects.create(
        workspace_id=ws.id, agent_type="PLANNER", action_type="set_label",
        risk_level="AUTO", status="applied", reasoning="r",
    )
    c = APIClient()
    c.force_authenticate(owner)
    resp = c.get(f"/api/ai/workspaces/{ws.id}/orchestrator/actions/")
    assert resp.status_code == 200
    assert len(resp.data["actions"]) >= 1


def test_kill_switch_endpoint(db, make_workspace, make_ai_config, make_user, make_workspace_member):
    owner = make_user("owner")
    ws = make_workspace(owner=owner)
    make_workspace_member(workspace=ws, user=owner, role=20)
    make_ai_config(ws)
    c = APIClient()
    c.force_authenticate(owner)
    resp = c.post(
        f"/api/ai/workspaces/{ws.id}/orchestrator/kill-switch/",
        {"engaged": True}, format="json",
    )
    assert resp.status_code == 200
    assert resp.data["engaged"] is True
    resp = c.get(f"/api/ai/workspaces/{ws.id}/orchestrator/kill-switch/")
    assert resp.data["engaged"] is True


# ---- Velocity ----------------------------------------------------------


def test_velocity_records_completion_idempotently(
    db, make_workspace, make_ai_config, make_project, make_issue, make_user
):
    from ai.orchestrator import velocity
    owner = make_user("owner")
    ws = make_workspace(owner=owner)
    make_ai_config(ws)
    prj = make_project(workspace=ws, created_by=owner)
    issue = make_issue(workspace=ws, project=prj, name="finished")
    velocity.record_completion(issue)
    velocity.record_completion(issue)  # re-record
    assert TeamVelocity.objects.filter(issue_id=issue.id).count() == 1
