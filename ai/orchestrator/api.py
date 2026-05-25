"""DRF views for orchestrator endpoints (TZ 12.1 — Goals UI + Activity feed).

Endpoints (all under ``/api/ai/workspaces/<ws>/orchestrator/``):

  GET    goals/                    list goals in workspace
  POST   goals/                    create + run PLANNER inline
  GET    goals/<id>/               goal detail with plan_preview
  POST   goals/<id>/apply/         apply plan_preview to a project (creates issues)
  POST   goals/<id>/report/        generate weekly status report
  GET    actions/                  activity feed (AgentAction rows)
  GET    risks/                    open PredictedRisk rows
  POST   risks/<id>/resolve/       mark risk resolved
  GET    kill-switch/              { engaged: bool }
  POST   kill-switch/              { engaged: bool } → flip
  POST   trigger/scan/             manually scan a project for risks
  POST   trigger/analyst/          generate insight report now
"""

from __future__ import annotations

import logging
from datetime import date as date_cls

from django.apps import apps
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from ai.acl import ROLE, allowed_projects
from ai.models import (
    AgentAction,
    PredictedRisk,
    ProjectGoal,
    WorkspaceAIConfig,
)
from ai.views import _is_workspace_member, _user_can_use_ai
from . import analyst, communicator, monitor, planner
from .breaker import engage_kill_switch, release_kill_switch


logger = logging.getLogger("plane.ai.orchestrator.api")


def _is_ws_admin(user, workspace_id) -> bool:
    WorkspaceMember = apps.get_model("db", "WorkspaceMember")
    return WorkspaceMember.objects.filter(
        member=user, workspace_id=workspace_id, role=ROLE.ADMIN.value,
        is_active=True, deleted_at__isnull=True,
    ).exists()


def _goal_to_dict(g: ProjectGoal) -> dict:
    return {
        "id": str(g.id),
        "workspace_id": str(g.workspace_id),
        "project_id": str(g.project_id) if g.project_id else None,
        "title": g.title,
        "description": g.description,
        "deadline": g.deadline.isoformat() if g.deadline else None,
        "constraints": g.constraints,
        "status": g.status,
        "plan_preview": g.plan_preview,
        "plan_issue_count": len(g.plan_issue_ids or []),
        "plan_issue_ids": g.plan_issue_ids or [],
        "created_at": g.created_at.isoformat() if g.created_at else None,
        "updated_at": g.updated_at.isoformat() if g.updated_at else None,
    }


def _action_to_dict(a: AgentAction) -> dict:
    return {
        "id": str(a.id),
        "agent_type": a.agent_type,
        "action_type": a.action_type,
        "status": a.status,
        "risk_level": a.risk_level,
        "reasoning": a.reasoning,
        "input": a.input,
        "output": a.output,
        "project_id": str(a.project_id) if a.project_id else None,
        "goal_id": str(a.goal_id) if a.goal_id else None,
        "target_issue_id": str(a.target_issue_id) if a.target_issue_id else None,
        "created_at": a.created_at.isoformat(),
    }


def _risk_to_dict(r: PredictedRisk) -> dict:
    return {
        "id": str(r.id),
        "issue_id": str(r.issue_id),
        "project_id": str(r.project_id),
        "risk_type": r.risk_type,
        "impact": r.impact,
        "confidence": r.confidence,
        "rationale": r.rationale,
        "suggested_actions": r.suggested_actions,
        "resolved": r.resolved,
        "escalated_at": r.escalated_at.isoformat() if r.escalated_at else None,
        "created_at": r.created_at.isoformat(),
    }


# ---- Goals ---------------------------------------------------------------


class GoalListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, workspace_id):
        if not _is_workspace_member(request.user, workspace_id):
            return Response({"error": "not_a_member"}, status=403)
        goals = ProjectGoal.objects.filter(workspace_id=workspace_id).order_by("-created_at")[:100]
        return Response({"goals": [_goal_to_dict(g) for g in goals]})

    def post(self, request, workspace_id):
        ok, err, cfg, http = _user_can_use_ai(request.user, workspace_id)
        if not ok:
            return Response({"error": err}, status=http)
        title = (request.data.get("title") or "").strip()
        if not title:
            return Response({"error": "title required"}, status=400)
        deadline_raw = request.data.get("deadline")
        try:
            deadline = date_cls.fromisoformat(deadline_raw) if deadline_raw else None
        except (TypeError, ValueError):
            return Response({"error": "bad deadline"}, status=400)
        constraints = request.data.get("constraints") or {}
        if not isinstance(constraints, dict):
            constraints = {}
        project_ref = request.data.get("project")  # optional — bound to a project up front
        project_id = None
        if project_ref:
            from ai.agent_tools import _resolve_project, ToolError
            try:
                project_id = _resolve_project(workspace_id, project_ref).id
            except ToolError as e:
                return Response({"error": str(e)}, status=400)

        goal = ProjectGoal.objects.create(
            workspace_id=workspace_id,
            project_id=project_id,
            title=title[:255],
            description=(request.data.get("description") or "")[:5000],
            deadline=deadline,
            constraints=constraints,
            created_by=request.user,
        )
        run_planner = bool(request.data.get("run_planner", True))
        plan_summary = None
        if run_planner:
            try:
                plan, _action = planner.decompose_goal(goal=goal, cfg=cfg)
                plan_summary = {
                    "epic_count": len(plan["epics"]),
                    "task_count": plan["task_count"],
                    "summary": plan["summary"],
                }
            except Exception as exc:
                logger.exception("planner failed on goal %s", goal.id)
                plan_summary = {"error": f"{type(exc).__name__}: {exc}"}
        return Response(
            {"goal": _goal_to_dict(goal), "plan_summary": plan_summary},
            status=201,
        )


class GoalDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, workspace_id, goal_id):
        if not _is_workspace_member(request.user, workspace_id):
            return Response({"error": "not_a_member"}, status=403)
        goal = ProjectGoal.objects.filter(id=goal_id, workspace_id=workspace_id).first()
        if goal is None:
            return Response({"error": "not_found"}, status=404)
        return Response({"goal": _goal_to_dict(goal)})


class GoalApplyView(APIView):
    """POST → create issues from plan_preview into target project."""
    permission_classes = [IsAuthenticated]

    def post(self, request, workspace_id, goal_id):
        ok, err, cfg, http = _user_can_use_ai(request.user, workspace_id)
        if not ok:
            return Response({"error": err}, status=http)
        goal = ProjectGoal.objects.filter(id=goal_id, workspace_id=workspace_id).first()
        if goal is None:
            return Response({"error": "not_found"}, status=404)
        project_ref = request.data.get("project") or goal.project_id
        if not project_ref:
            return Response({"error": "project required"}, status=400)
        from ai.agent_tools import ToolError
        try:
            result = planner.apply_plan(goal=goal, user=request.user, project_ref=project_ref)
        except ToolError as e:
            return Response({"error": str(e)}, status=400)
        return Response({"applied": result, "goal": _goal_to_dict(goal)}, status=201)


class GoalReportView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, workspace_id, goal_id):
        ok, err, cfg, http = _user_can_use_ai(request.user, workspace_id)
        if not ok:
            return Response({"error": err}, status=http)
        goal = ProjectGoal.objects.filter(id=goal_id, workspace_id=workspace_id).first()
        if goal is None:
            return Response({"error": "not_found"}, status=404)
        report = communicator.status_report(goal=goal, cfg=cfg)
        return Response({"report": report})


# ---- Activity feed -------------------------------------------------------


class ActivityFeedView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, workspace_id):
        if not _is_workspace_member(request.user, workspace_id):
            return Response({"error": "not_a_member"}, status=403)
        # Scope by allowed projects so a guest doesn't see actions on
        # projects they can't view.
        allowed = set(str(p) for p in allowed_projects(request.user, workspace_id))
        qs = AgentAction.objects.filter(workspace_id=workspace_id).order_by("-created_at")
        agent = request.GET.get("agent")
        if agent:
            qs = qs.filter(agent_type=agent.upper())
        try:
            limit = min(int(request.GET.get("limit", 50)), 200)
        except (TypeError, ValueError):
            limit = 50
        rows = list(qs[:limit])
        rows = [r for r in rows if (r.project_id is None or str(r.project_id) in allowed)]
        return Response({"actions": [_action_to_dict(r) for r in rows]})


# ---- Risks --------------------------------------------------------------


class RiskListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, workspace_id):
        if not _is_workspace_member(request.user, workspace_id):
            return Response({"error": "not_a_member"}, status=403)
        allowed = set(str(p) for p in allowed_projects(request.user, workspace_id))
        qs = PredictedRisk.objects.filter(workspace_id=workspace_id, resolved=False).order_by("-created_at")
        rows = [r for r in qs[:200] if str(r.project_id) in allowed]
        return Response({"risks": [_risk_to_dict(r) for r in rows]})


class RiskResolveView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, workspace_id, risk_id):
        if not _is_workspace_member(request.user, workspace_id):
            return Response({"error": "not_a_member"}, status=403)
        risk = PredictedRisk.objects.filter(id=risk_id, workspace_id=workspace_id).first()
        if risk is None:
            return Response({"error": "not_found"}, status=404)
        risk.resolved = True
        risk.save(update_fields=["resolved", "updated_at"])
        return Response({"risk": _risk_to_dict(risk)})


# ---- Kill switch --------------------------------------------------------


class KillSwitchView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, workspace_id):
        if not _is_workspace_member(request.user, workspace_id):
            return Response({"error": "not_a_member"}, status=403)
        cfg = WorkspaceAIConfig.objects.filter(workspace_id=workspace_id).only(
            "agents_killed", "max_agent_actions_per_hour"
        ).first()
        return Response({
            "engaged": bool(cfg and cfg.agents_killed),
            "max_agent_actions_per_hour": cfg.max_agent_actions_per_hour if cfg else None,
        })

    def post(self, request, workspace_id):
        if not _is_ws_admin(request.user, workspace_id):
            return Response({"error": "admin_required"}, status=403)
        engaged = bool(request.data.get("engaged"))
        if engaged:
            engage_kill_switch(workspace_id, reason=f"manual by {request.user.id}")
        else:
            release_kill_switch(workspace_id)
        return Response({"engaged": engaged})


# ---- Manual triggers -----------------------------------------------------


class TriggerScanView(APIView):
    """POST /trigger/scan/ {project_id} — run MONITOR + ESCALATOR now."""
    permission_classes = [IsAuthenticated]

    def post(self, request, workspace_id):
        ok, err, cfg, http = _user_can_use_ai(request.user, workspace_id)
        if not ok:
            return Response({"error": err}, status=http)
        project_id = request.data.get("project_id")
        if not project_id:
            return Response({"error": "project_id required"}, status=400)
        scan = monitor.scan_project(workspace_id, project_id)
        # Escalate critical risks inline
        from . import escalator
        crit = [
            r for r in PredictedRisk.objects.filter(
                id__in=scan.get("risk_ids", []),
                impact=PredictedRisk.IMPACT_CRITICAL,
                escalated_at__isnull=True,
                resolved=False,
            )
        ]
        escal = escalator.escalate_critical_risks([str(r.id) for r in crit], cfg=cfg)
        return Response({"scan": scan, "escalation": escal})


class TriggerAnalystView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, workspace_id):
        ok, err, cfg, http = _user_can_use_ai(request.user, workspace_id)
        if not ok:
            return Response({"error": err}, status=http)
        days = int(request.data.get("days", 30))
        project_id = request.data.get("project_id")
        result = analyst.generate_insight(
            workspace_id=workspace_id, project_id=project_id, cfg=cfg, days=days
        )
        return Response(result)
