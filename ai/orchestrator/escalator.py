"""ESCALATOR agent (TZ 9.2).

Takes a critical-impact ``PredictedRisk`` and surfaces it to humans:
posts a comment on the affected issue with 3 actionable options +
a recommendation. Marks the risk as ``escalated_at = now`` so the
weekly tick doesn't re-escalate.

Options come from the risk's ``suggested_actions`` (precomputed by
MONITOR), optionally enriched by a short LLM pass that turns them
into a Russian comment body with costs + recommendation.
"""

from __future__ import annotations

import logging
from typing import Iterable
from uuid import UUID

from django.apps import apps
from django.utils import timezone

from ai.models import AgentAction, PredictedRisk, WorkspaceAIConfig
from .base import log_action
from .breaker import ensure_agents_allowed
from .llm import ask_text
import html


logger = logging.getLogger("plane.ai.orchestrator.escalator")

AGENT = AgentAction.AGENT_ESCALATOR


ESCALATOR_SYSTEM = """Ты — ESCALATOR. Получаешь риск задачи и список вариантов решения. \
Напиши короткий комментарий (5-10 строк) на русском: \
1) что произошло, 2) три варианта с трейд-офф, 3) рекомендация. \
Никаких выдуманных людей, цифр или дедлайнов. \
Только markdown, без обёрток. Заголовки — жирным, не #."""


def _render_options_inline(actions: list[dict]) -> str:
    lines = []
    for i, a in enumerate(actions[:3], 1):
        cost = a.get("cost", "")
        label = a.get("label", a.get("id", ""))
        lines.append(f"{i}. **{label}**" + (f" (стоимость: {cost})" if cost else ""))
    return "\n".join(lines)


def escalate(risk: PredictedRisk, cfg: WorkspaceAIConfig | None = None) -> dict:
    """Post the escalation comment + log the action."""
    if risk.resolved:
        return {"skipped": "already_resolved"}
    if risk.escalated_at is not None:
        return {"skipped": "already_escalated"}

    ensure_agents_allowed(risk.workspace_id)

    Issue = apps.get_model("db", "Issue")
    IssueComment = apps.get_model("db", "IssueComment")
    issue = (
        Issue.objects.filter(id=risk.issue_id, deleted_at__isnull=True)
        .select_related("project", "created_by")
        .first()
    )
    if issue is None:
        return {"skipped": "issue_missing"}

    actions = risk.suggested_actions or []

    body: str | None = None
    if cfg and cfg.anthropic_key and actions:
        try:
            body = ask_text(
                workspace_id=risk.workspace_id,
                cfg=cfg,
                system=ESCALATOR_SYSTEM,
                user=(
                    f"Задача: {issue.name}\n"
                    f"Риск: {risk.risk_type} (уверенность {risk.confidence:.0%}, влияние {risk.impact})\n"
                    f"Причина: {risk.rationale}\n"
                    f"Варианты: {actions}\n"
                ),
                cheap=True,
            )
        except Exception as exc:
            logger.warning("escalator LLM failed, using template: %s", exc)
            body = None

    if not body:
        body = (
            f"🚨 **Риск:** {risk.risk_type} ({risk.impact}, уверенность {risk.confidence:.0%})\n"
            f"_Причина:_ {risk.rationale}\n\n"
            f"**Варианты решения:**\n"
            f"{_render_options_inline(actions)}\n\n"
            f"⭐ Нужно решение PM"
        )

    actor = None
    comment = IssueComment.objects.create(
        project_id=issue.project_id,
        issue_id=issue.id,
        actor=actor,
        comment_stripped=body[:5000],
        comment_html=f"<p>{html.escape(body[:5000])}</p>",
        access="INTERNAL",
    )

    risk.escalated_at = timezone.now()
    risk.save(update_fields=["escalated_at", "updated_at"])

    log_action(
        workspace_id=risk.workspace_id,
        project_id=risk.project_id,
        target_issue_id=risk.issue_id,
        agent_type=AGENT,
        action_type="escalate_to_pm",
        input={"risk_id": str(risk.id), "risk_type": risk.risk_type, "impact": risk.impact},
        output={"comment_id": str(comment.id), "option_count": len(actions)},
        reasoning=risk.rationale,
    )
    return {"comment_id": str(comment.id), "risk_id": str(risk.id)}


def escalate_critical_risks(risk_ids: Iterable[str], cfg: WorkspaceAIConfig | None) -> dict:
    """Bulk helper: escalate a list of risk ids (typically what
    MONITOR.scan_project returned)."""
    ids = list(risk_ids)
    if not ids:
        return {"escalated": 0, "skipped": 0}
    risks = {str(r.id): r for r in PredictedRisk.objects.filter(id__in=ids)}
    escalated = []
    skipped = []
    for rid in ids:
        risk = risks.get(str(rid))
        if risk is None:
            continue
        out = escalate(risk, cfg=cfg)
        if "comment_id" in out:
            escalated.append(out)
        else:
            skipped.append(out)
    return {"escalated": len(escalated), "skipped": len(skipped)}
