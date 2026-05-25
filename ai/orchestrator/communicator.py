"""COMMUNICATOR agent (TZ 10.2).

Weekly status digest per goal: progress %, in-flight + completed
items, open critical risks, deadline outlook. Calls LLM only to
prettify; the numbers come from the DB.

Output is one ``AgentAction`` row with ``action_type='weekly_status_report'``
holding the markdown narrative — UI renders directly from the row.
No e-mail sending in MVP (CLAUDE.md note about reactive scope).
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from django.apps import apps
from django.utils import timezone

from ai.models import AgentAction, PredictedRisk, ProjectGoal, WorkspaceAIConfig
from .base import log_action
from .breaker import ensure_agents_allowed
from .llm import ask_text


logger = logging.getLogger("plane.ai.orchestrator.communicator")

AGENT = AgentAction.AGENT_COMMUNICATOR


COMM_SYSTEM = """Ты — COMMUNICATOR. На вход получаешь статистику по цели: \
сколько задач сделано, сколько в работе, открытые риски, дедлайн. \
Напиши короткий отчёт по шаблону (маркдаун, на русском, 6-10 строк):

**Цель:** ...
**Прогресс:** N% (на треке 🟢 / под риском 🟡 / срыв 🔴)
**Завершено:** ...
**В работе:** ...
**Риски:** ...
**Дедлайн:** ...

Цифры — только из присланных данных, никаких выдумок."""


def _goal_stats(goal: ProjectGoal) -> dict[str, Any]:
    Issue = apps.get_model("db", "Issue")
    issue_ids = goal.plan_issue_ids or []
    total = len(issue_ids)
    if total == 0:
        return {"total": 0, "completed": 0, "in_progress": 0, "open_risks": []}
    issues = list(
        Issue.objects.filter(id__in=issue_ids, deleted_at__isnull=True)
        .select_related("state")
    )
    completed = sum(
        1 for i in issues if getattr(getattr(i, "state", None), "group", "") == "completed"
    )
    in_progress = sum(
        1 for i in issues if getattr(getattr(i, "state", None), "group", "") == "started"
    )
    risks = list(
        PredictedRisk.objects.filter(
            workspace_id=goal.workspace_id,
            issue_id__in=issue_ids,
            resolved=False,
        ).values("risk_type", "impact", "rationale")[:10]
    )
    return {
        "total": total,
        "completed": completed,
        "in_progress": in_progress,
        "open_risks": risks,
        "progress_pct": int(100 * completed / total) if total else 0,
    }


def status_report(*, goal: ProjectGoal, cfg: WorkspaceAIConfig | None) -> dict:
    """Produce one report for one goal. Returns the persisted action."""
    ensure_agents_allowed(goal.workspace_id)
    stats = _goal_stats(goal)

    deadline = goal.deadline.isoformat() if goal.deadline else "не задан"
    summary_input = (
        f"Цель: {goal.title}\n"
        f"Дедлайн: {deadline}\n"
        f"Всего задач: {stats['total']}, завершено: {stats['completed']}, "
        f"в работе: {stats['in_progress']}\n"
        f"Открытые риски: {stats['open_risks']}"
    )

    text: str
    if cfg and cfg.anthropic_key and stats["total"] > 0:
        try:
            text = ask_text(
                workspace_id=goal.workspace_id,
                cfg=cfg,
                system=COMM_SYSTEM,
                user=summary_input,
                cheap=True,
            )
        except Exception as exc:
            logger.warning("communicator LLM failed: %s", exc)
            text = _fallback_report(goal, stats)
    else:
        text = _fallback_report(goal, stats)

    action = log_action(
        workspace_id=goal.workspace_id,
        project_id=goal.project_id,
        goal_id=goal.id,
        agent_type=AGENT,
        action_type="weekly_status_report",
        input={"stats": stats},
        output={"narrative": text[:2500]},
        reasoning=f"weekly status for goal {goal.id}",
    )
    return {"narrative": text, "action_id": str(action.id), "stats": stats}


def _fallback_report(goal: ProjectGoal, stats: dict) -> str:
    deadline = goal.deadline.isoformat() if goal.deadline else "не задан"
    if stats["total"] == 0:
        return (
            f"**Цель:** {goal.title}\n"
            f"**Прогресс:** план ещё не применён к проекту.\n"
            f"**Дедлайн:** {deadline}"
        )
    pct = stats["progress_pct"]
    light = "🟢" if pct >= 60 else ("🟡" if pct >= 25 else "🔴")
    risk_lines = "\n".join(
        f"- {r['risk_type']} / {r['impact']}: {r['rationale'][:120]}"
        for r in stats["open_risks"][:5]
    ) or "_открытых рисков нет_"
    return (
        f"**Цель:** {goal.title}\n"
        f"**Прогресс:** {pct}% {light}\n"
        f"**Завершено:** {stats['completed']} из {stats['total']}\n"
        f"**В работе:** {stats['in_progress']}\n"
        f"**Риски:**\n{risk_lines}\n"
        f"**Дедлайн:** {deadline}"
    )
