"""MONITOR agent (TZ 8.3).

Heuristic risk detection (no ML in MVP, per CLAUDE.md cold-start
note). For each active issue we score against three signals:

  1. **Time vs progress** — if more than 70% of the estimate's
     time-window has elapsed but the state is still "in progress"
     or earlier, flag as ``delay`` with confidence 0.7-0.9.
  2. **Blocked state / label** — if the issue carries a
     ``blocked``/``blocker`` label or is in a state with the
     ``cancelled``/``blocked`` group, flag as ``blocker`` (0.95).
  3. **Overload** — if the assignee has > N open issues
     (default 12) flag the issue as ``overload`` (0.6).

Each detection writes/updates a ``PredictedRisk`` row via the
unique-when-open constraint (one risk per (issue, type) at a time).
Critical-impact risks return a list the caller hands to ESCALATOR.

The scan is enqueued by the event router on `ISSUE_UPDATED` events
(debounced per project, so a flurry of updates triggers one scan).
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Iterable
from uuid import UUID

from django.apps import apps
from django.db.models import Count, Q
from django.utils import timezone

from ai.models import AgentAction, PredictedRisk
from .base import log_action
from .breaker import ensure_agents_allowed


logger = logging.getLogger("plane.ai.orchestrator.monitor")


AGENT = AgentAction.AGENT_MONITOR

# Overload threshold — number of open issues assigned to a single user.
OVERLOAD_OPEN_ISSUES = 12

# Delay heuristic — fraction of an issue's nominal window that has
# elapsed before we call it 'at risk'. Without estimates we fall back
# to project-default of 5 working days.
DELAY_WINDOW_DAYS_DEFAULT = 5
DELAY_TRIGGER_FRACTION = 0.7

BLOCKED_LABEL_NAMES = {"blocked", "blocker", "stuck"}


def _impact_for(issue, confidence: float) -> str:
    """Coarse impact heuristic — urgent/high priority → critical,
    medium → high, otherwise medium."""
    priority = (getattr(issue, "priority", "") or "").lower()
    if priority in {"urgent", "high"}:
        return PredictedRisk.IMPACT_CRITICAL if confidence >= 0.7 else PredictedRisk.IMPACT_HIGH
    if priority == "medium":
        return PredictedRisk.IMPACT_HIGH if confidence >= 0.8 else PredictedRisk.IMPACT_MEDIUM
    return PredictedRisk.IMPACT_MEDIUM


def _state_is_open(issue) -> bool:
    """Plane state has a ``group`` field — 'completed' / 'cancelled'
    mean done. Anything else is open."""
    state = getattr(issue, "state", None)
    group = getattr(state, "group", "") or ""
    return group not in {"completed", "cancelled"}


def _issue_labels(issue) -> set[str]:
    try:
        return {n.lower() for n in issue.labels.values_list("name", flat=True)}
    except Exception:
        return set()


def _detect_delay(issue) -> tuple[float, str] | None:
    if not _state_is_open(issue):
        return None
    target = getattr(issue, "target_date", None)
    start = getattr(issue, "start_date", None) or getattr(issue, "created_at", None)
    now = timezone.now()
    if target and start:
        try:
            total = (target - start).total_seconds()
            elapsed = (now - start).total_seconds()
            if total <= 0:
                return None
            frac = elapsed / total
            if frac >= DELAY_TRIGGER_FRACTION:
                conf = min(0.5 + frac * 0.4, 0.95)
                return conf, f"прошло {int(frac*100)}% времени до дедлайна, задача ещё открыта"
        except Exception:
            return None
    elif start:
        try:
            days = (now - start).total_seconds() / 86400.0
            if days >= DELAY_WINDOW_DAYS_DEFAULT * DELAY_TRIGGER_FRACTION:
                conf = min(0.5 + days / (DELAY_WINDOW_DAYS_DEFAULT * 2), 0.9)
                return conf, f"задача висит {int(days)} дней без дедлайна"
        except Exception:
            return None
    return None


def _detect_blocker(issue) -> tuple[float, str] | None:
    labels = _issue_labels(issue)
    if labels & BLOCKED_LABEL_NAMES:
        return 0.95, f"на задаче метка: {labels & BLOCKED_LABEL_NAMES}"
    return None


def _detect_overload(issue) -> tuple[float, str] | None:
    Issue = apps.get_model("db", "Issue")
    assignees = list(issue.assignees.values_list("id", flat=True))
    if not assignees:
        return None
    open_per_user = (
        Issue.objects.filter(
            assignees__id__in=assignees,
            deleted_at__isnull=True,
        )
        .exclude(state__group__in=("completed", "cancelled"))
        .values("assignees__id")
        .annotate(n=Count("id"))
    )
    overloaded = [r for r in open_per_user if r["n"] >= OVERLOAD_OPEN_ISSUES]
    if overloaded:
        max_n = max(r["n"] for r in overloaded)
        conf = min(0.4 + (max_n - OVERLOAD_OPEN_ISSUES) * 0.05, 0.85)
        return conf, f"исполнитель ведёт {max_n} открытых задач"
    return None


def _upsert_risk(issue, risk_type: str, confidence: float, impact: str, rationale: str) -> PredictedRisk:
    """Create or update an open risk row by (issue_id, risk_type)."""
    risk, _ = PredictedRisk.objects.update_or_create(
        issue_id=issue.id,
        risk_type=risk_type,
        resolved=False,
        defaults={
            "workspace_id": issue.workspace_id,
            "project_id": issue.project_id,
            "confidence": confidence,
            "impact": impact,
            "rationale": rationale,
            "suggested_actions": _suggest_actions(risk_type, impact),
        },
    )
    return risk


def _suggest_actions(risk_type: str, impact: str) -> list[dict]:
    """Coarse action library. ESCALATOR may rewrite these with LLM
    rationale before surfacing to a human."""
    base = {
        "delay": [
            {"id": "extend_deadline", "label": "Перенести дедлайн", "cost": "none"},
            {"id": "split_task", "label": "Разбить на подзадачи", "cost": "low"},
            {"id": "reassign", "label": "Перераспределить", "cost": "low"},
            {"id": "hire_freelancer", "label": "Нанять фрилансера", "cost": "high"},
        ],
        "blocker": [
            {"id": "unblock_dependency", "label": "Разблокировать зависимость", "cost": "low"},
            {"id": "drop_dependency", "label": "Отказаться от зависимости", "cost": "medium"},
        ],
        "overload": [
            {"id": "rebalance", "label": "Перераспределить нагрузку", "cost": "low"},
            {"id": "deprioritise", "label": "Снизить приоритет части задач", "cost": "low"},
        ],
        "dependency": [
            {"id": "expedite_blocker", "label": "Ускорить блокирующую задачу", "cost": "medium"},
        ],
    }
    return base.get(risk_type, [])


def scan_project(workspace_id: UUID | str, project_id: UUID | str) -> dict:
    """Walk active issues in the project, detect + upsert risks.

    Returns a summary the router can hand to ESCALATOR for any
    critical-impact risks."""
    ensure_agents_allowed(workspace_id)
    Issue = apps.get_model("db", "Issue")
    issues = list(
        Issue.objects.filter(
            workspace_id=workspace_id,
            project_id=project_id,
            deleted_at__isnull=True,
            is_draft=False,
        )
        .exclude(state__group__in=("completed", "cancelled"))
        .select_related("state")
        .prefetch_related("assignees", "labels")[:500]
    )

    risks_found: list[PredictedRisk] = []
    critical_issue_ids: list[str] = []
    detectors = [
        (PredictedRisk.TYPE_DELAY, _detect_delay),
        (PredictedRisk.TYPE_BLOCKER, _detect_blocker),
        (PredictedRisk.TYPE_OVERLOAD, _detect_overload),
    ]

    for issue in issues:
        for risk_type, fn in detectors:
            hit = fn(issue)
            if hit is None:
                continue
            confidence, rationale = hit
            impact = _impact_for(issue, confidence)
            risk = _upsert_risk(issue, risk_type, confidence, impact, rationale)
            risks_found.append(risk)
            if impact == PredictedRisk.IMPACT_CRITICAL:
                critical_issue_ids.append(str(issue.id))

    log_action(
        workspace_id=workspace_id,
        project_id=project_id,
        agent_type=AGENT,
        action_type="record_risk",
        input={"issue_count": len(issues)},
        output={
            "risks_recorded": len(risks_found),
            "critical_issue_ids": critical_issue_ids[:50],
        },
        reasoning=f"scanned {len(issues)} issues, found {len(risks_found)} risks",
    )
    return {
        "scanned": len(issues),
        "risks": len(risks_found),
        "critical_issue_ids": critical_issue_ids,
        "risk_ids": [str(r.id) for r in risks_found],
    }
