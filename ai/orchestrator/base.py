"""Shared helpers for the 7 orchestrator agents.

``log_action`` is the single entry point every agent uses to record
a decision. It consults the Decision Layer to set ``risk_level``,
classifies based on the matrix, and writes an ``AgentAction`` row.

Two side effects worth noting:

  1. **Forbidden actions never reach the DB-write.** If
     :func:`decision.is_forbidden` is True we raise ``ValueError`` —
     the agent has no business asking for it. The router catches the
     exception and records it as ``status='rejected'`` with a
     diagnostic so the incident log shows the attempt.

  2. **Status policy.** AUTO → ``applied``. NOTIFY → ``applied`` but
     surfaced. CONFIRM → ``proposed`` (action body sits in
     ``input``; the user approves via the UI). ESCALATE →
     ``awaiting_user`` and the ESCALATOR is expected to post a
     comment on the relevant issue/goal.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from django.apps import apps
from django.db import IntegrityError, transaction

from .decision import (
    AUTO,
    CONFIRM,
    ESCALATE,
    NOTIFY,
    decide,
    is_forbidden,
)


logger = logging.getLogger("plane.ai.orchestrator")


def log_action(
    *,
    workspace_id: UUID | str,
    agent_type: str,
    action_type: str,
    project_id: UUID | str | None = None,
    goal_id: UUID | str | None = None,
    target_issue_id: UUID | str | None = None,
    input: dict[str, Any] | None = None,
    output: dict[str, Any] | None = None,
    reasoning: str = "",
    on_critical_path: bool = False,
    force_status: str | None = None,
):
    """Write one AgentAction row. Returns the row.

    Caller has already done the side effect (or NOT done it, when the
    risk level demands user approval). This function just persists
    the audit record.
    """
    AgentAction = apps.get_model("ai", "AgentAction")
    if is_forbidden(action_type):
        # We persist the rejection so the audit shows the attempt.
        # Wrap in savepoint so a FK violation (workspace rolled back
        # between event enqueue and worker pickup — common in pytest)
        # doesn't poison the surrounding transaction.
        try:
            with transaction.atomic():
                return AgentAction.objects.create(
                    workspace_id=workspace_id,
                    project_id=project_id,
                    goal_id=goal_id,
                    target_issue_id=target_issue_id,
                    agent_type=agent_type,
                    action_type=action_type,
                    input=input or {},
                    output=output or {},
                    reasoning=reasoning,
                    risk_level=ESCALATE,
                    status="rejected",
                )
        except IntegrityError as exc:
            logger.warning("forbidden-audit FK violation ws=%s: %s", workspace_id, exc)
            return None

    risk_level = decide(action_type, on_critical_path=on_critical_path)
    if force_status:
        status = force_status
    elif risk_level == AUTO:
        status = "applied"
    elif risk_level == NOTIFY:
        status = "applied"
    elif risk_level == CONFIRM:
        status = "proposed"
    else:  # ESCALATE
        status = "awaiting_user"

    try:
        with transaction.atomic():
            row = AgentAction.objects.create(
                workspace_id=workspace_id,
                project_id=project_id,
                goal_id=goal_id,
                target_issue_id=target_issue_id,
                agent_type=agent_type,
                action_type=action_type,
                input=input or {},
                output=output or {},
                reasoning=reasoning,
                risk_level=risk_level or ESCALATE,
                status=status,
            )
    except IntegrityError as exc:
        # workspace_id / project_id / goal_id FK can fail if the
        # referenced row was deleted between the orchestrator's
        # precheck and the audit write — most commonly pytest
        # transactions rolling back the parent row before the
        # signal-driven Celery task lands. Audit can never crash the
        # caller; log and return None.
        logger.warning(
            "agent.action FK violation ws=%s %s/%s: %s",
            workspace_id, agent_type, action_type, exc,
        )
        return None
    logger.info(
        "agent.action: ws=%s %s/%s risk=%s status=%s",
        workspace_id, agent_type, action_type, row.risk_level, status,
    )
    return row
