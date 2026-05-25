"""ANALYST agent (TZ 10.1).

Aggregates ``TeamVelocity`` rows to detect bottlenecks: who's slowest
per task_type, where estimates are systematically off, what's
clogging the pipeline. Emits a short text insight (Russian) that
COMMUNICATOR can pull into a weekly report, plus a structured
``AgentAction`` row the UI uses.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any
from uuid import UUID

from django.db.models import Avg, Count, F, FloatField, Q
from django.db.models.functions import Coalesce
from django.utils import timezone

from ai.models import AgentAction, TeamVelocity, WorkspaceAIConfig
from .base import log_action
from .breaker import ensure_agents_allowed
from .llm import ask_text


logger = logging.getLogger("plane.ai.orchestrator.analyst")

AGENT = AgentAction.AGENT_ANALYST


def _aggregate(workspace_id, project_id=None, days: int = 60) -> dict[str, Any]:
    cutoff = timezone.now() - timedelta(days=days)
    qs = TeamVelocity.objects.filter(
        workspace_id=workspace_id, completed_at__gte=cutoff
    )
    if project_id:
        qs = qs.filter(project_id=project_id)

    by_user = list(
        qs.values("user_id", "user__email")
        .annotate(
            samples=Count("id"),
            avg_actual=Avg("actual_hours"),
            avg_estimate=Avg("estimated_hours"),
        )
        .order_by("-avg_actual")[:20]
    )
    by_type = list(
        qs.values("task_type")
        .annotate(
            samples=Count("id"),
            avg_actual=Avg("actual_hours"),
            avg_estimate=Avg("estimated_hours"),
        )
        .order_by("-avg_actual")[:10]
    )
    return {
        "window_days": days,
        "samples": qs.count(),
        "by_user": by_user,
        "by_type": by_type,
    }


ANALYST_SYSTEM = """Ты — ANALYST. Получаешь агрегаты velocity команды (часы план/факт по \
пользователям и типам задач). Напиши краткую сводку (5-8 строк, маркдаун, на русском): \
1) что выделяется (узкие места, систематический перерасход), \
2) одно конкретное предложение по улучшению. \
Не выдумывай данные, опирайся только на присланную статистику."""


def generate_insight(
    *, workspace_id: UUID | str, project_id=None, cfg: WorkspaceAIConfig | None = None, days: int = 60
) -> dict:
    """Run aggregate + LLM narrative. Stored as an ``insight_report``
    AgentAction row."""
    ensure_agents_allowed(workspace_id)
    stats = _aggregate(workspace_id, project_id=project_id, days=days)

    if stats["samples"] < 3:
        text = (
            "Данных пока мало — собрано "
            f"{stats['samples']} завершённых задач за {days} дней. "
            "Через 1-2 спринта появятся первые паттерны."
        )
    elif cfg and cfg.anthropic_key:
        user = (
            f"Окно: последние {days} дней. Всего сэмплов: {stats['samples']}.\n"
            f"По пользователям: {stats['by_user'][:5]}\n"
            f"По типам задач: {stats['by_type'][:5]}"
        )
        try:
            text = ask_text(
                workspace_id=workspace_id,
                cfg=cfg,
                system=ANALYST_SYSTEM,
                user=user,
                cheap=True,
            )
        except Exception as exc:
            logger.warning("analyst LLM failed: %s", exc)
            text = f"Аналитика без LLM: всего {stats['samples']} завершённых задач за {days} дней."
    else:
        text = f"Собрано {stats['samples']} сэмплов velocity за {days} дней. Подключи LLM-ключ для нарратива."

    action = log_action(
        workspace_id=workspace_id,
        project_id=project_id,
        agent_type=AGENT,
        action_type="insight_report",
        input={"window_days": days, "samples": stats["samples"]},
        output={"narrative": text[:2000], "stats": stats},
        reasoning=text[:500],
    )
    return {"narrative": text, "action_id": str(action.id), "stats": stats}
