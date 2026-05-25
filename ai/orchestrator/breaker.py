"""Circuit breaker + kill-switch (TZ 11.2).

Two independent layers that together stop a runaway orchestrator:

  1. **Kill switch.** A single boolean on ``WorkspaceAIConfig``
     (``agents_killed``). Flipped from the UI / management command.
     When True, :func:`agents_allowed` is False — every router
     dispatch refuses early without consulting the breaker.

  2. **Circuit breaker.** Rate of agent actions over the trailing
     hour. If ``AgentAction.objects.filter(workspace=ws,
     created_at >= now-1h).count() >= config.max_agent_actions_per_hour``,
     :func:`breaker_open` returns True and no new action is allowed
     until older actions roll out of the window.

Both checks are cheap (one count() with an index) and called from the
router on every event. The reason the count includes both AUTO and
all other levels is intentional: an attacker who could trick the
matrix into AUTO for an action class would burn through the budget
just as fast as the legitimate case, so the breaker doesn't care
about classification.

The breaker is one-way "open" only — it auto-recovers when the hour
rolls forward. There is no manual "reset" because the natural decay
IS the reset.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from uuid import UUID

from django.apps import apps
from django.utils import timezone


logger = logging.getLogger("plane.ai.orchestrator.breaker")


class AgentsHalted(Exception):
    """Raised by ``ensure_agents_allowed`` when something stopped us.

    The message describes which layer blocked the action. Callers
    catch this and log it as an ``AgentAction`` with
    ``status='rejected'`` so the UI sees the refusal."""


def agents_allowed(workspace_id: UUID | str) -> bool:
    """Cheap precheck: is the kill switch off AND breaker closed?

    Two queries — one for the config, one for the count. Returns
    True if both pass."""
    WorkspaceAIConfig = apps.get_model("ai", "WorkspaceAIConfig")
    cfg = (
        WorkspaceAIConfig.objects.filter(workspace_id=workspace_id)
        .only("agents_killed", "max_agent_actions_per_hour", "enabled")
        .first()
    )
    if cfg is None or not cfg.enabled:
        return False
    if cfg.agents_killed:
        return False
    return not _breaker_open(workspace_id, cfg.max_agent_actions_per_hour)


def _breaker_open(workspace_id, cap: int) -> bool:
    AgentAction = apps.get_model("ai", "AgentAction")
    cutoff = timezone.now() - timedelta(hours=1)
    count = AgentAction.objects.filter(
        workspace_id=workspace_id,
        created_at__gte=cutoff,
        # Only count applied/awaiting/proposed — failed and rejected
        # don't fire writes against Plane, so they shouldn't count
        # against the breaker.
        status__in=("applied", "proposed", "awaiting_user"),
    ).count()
    return count >= cap


def ensure_agents_allowed(workspace_id: UUID | str) -> None:
    """Raise :class:`AgentsHalted` with a precise reason if a layer
    is blocking. The router uses this as the gate before any agent
    can act."""
    WorkspaceAIConfig = apps.get_model("ai", "WorkspaceAIConfig")
    cfg = (
        WorkspaceAIConfig.objects.filter(workspace_id=workspace_id)
        .only("agents_killed", "max_agent_actions_per_hour", "enabled")
        .first()
    )
    if cfg is None:
        raise AgentsHalted("no_ai_config_for_workspace")
    if not cfg.enabled:
        raise AgentsHalted("ai_disabled_for_workspace")
    if cfg.agents_killed:
        logger.warning("agents halted by kill-switch: ws=%s", workspace_id)
        raise AgentsHalted("kill_switch_engaged")
    if _breaker_open(workspace_id, cfg.max_agent_actions_per_hour):
        logger.warning(
            "agents halted by circuit breaker: ws=%s cap=%d",
            workspace_id,
            cfg.max_agent_actions_per_hour,
        )
        raise AgentsHalted("circuit_breaker_open")


def engage_kill_switch(workspace_id: UUID | str, *, reason: str = "") -> None:
    """Set ``agents_killed=True`` on the config. Idempotent."""
    WorkspaceAIConfig = apps.get_model("ai", "WorkspaceAIConfig")
    n = WorkspaceAIConfig.objects.filter(workspace_id=workspace_id).update(
        agents_killed=True
    )
    if n:
        logger.warning(
            "kill switch engaged: ws=%s reason=%s", workspace_id, reason
        )


def release_kill_switch(workspace_id: UUID | str) -> None:
    """Set ``agents_killed=False`` on the config. Idempotent."""
    WorkspaceAIConfig = apps.get_model("ai", "WorkspaceAIConfig")
    WorkspaceAIConfig.objects.filter(workspace_id=workspace_id).update(
        agents_killed=False
    )
    logger.info("kill switch released: ws=%s", workspace_id)


__all__ = [
    "AgentsHalted",
    "agents_allowed",
    "ensure_agents_allowed",
    "engage_kill_switch",
    "release_kill_switch",
]
