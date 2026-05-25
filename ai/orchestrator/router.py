"""ORCHESTRATOR (TZ 11.1) — event router with Redis lock + idempotency.

This is the central coordinator. The Celery task
``orchestrator_handle_event`` (in ai/tasks.py) hands one ``Event``
here; we:

  1. **Gate.** If kill switch is engaged or breaker is open, log a
     rejection and return early.
  2. **Loop guard.** If the event carries ``modified_by_agent=True``,
     drop it — the agent that just wrote is responsible for any
     further dispatch.
  3. **Lock.** ``agent_lock:{issue_id}`` via Redis cache — only one
     orchestrator handler at a time per issue, so two simultaneous
     events on the same issue don't race.
  4. **Idempotency.** A per-event dedupe key on
     ``ai:event_seen:{type}:{id}:{stamp}`` — repeated delivery
     within 60s is dropped.
  5. **Route.** Maps event type → list of agents to wake. Each agent
     call is wrapped in try/except so one agent failure doesn't
     break the others.

Hop budget: a single event can fan out to at most ``MAX_HOPS`` agent
calls (default 4). MONITOR → ESCALATOR is the canonical 2-hop chain;
4 is plenty for any sane real-world fanout.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import timedelta
from typing import Any, Callable
from uuid import UUID

from django.core.cache import cache
from django.utils import timezone

from ai.models import AgentAction, WorkspaceAIConfig
from . import (
    analyst,
    communicator,
    escalator,
    events as ev,
    executor,
    monitor,
    velocity as vel,
)
from .base import log_action
from .breaker import AgentsHalted, ensure_agents_allowed


logger = logging.getLogger("plane.ai.orchestrator.router")

LOCK_TTL = 60
DEDUPE_TTL = 60
MAX_HOPS = 4


@contextmanager
def _issue_lock(issue_id):
    if not issue_id:
        yield True
        return
    key = f"agent_lock:{issue_id}"
    acquired = cache.add(key, "1", timeout=LOCK_TTL)
    try:
        yield acquired
    finally:
        if acquired:
            try:
                cache.delete(key)
            except Exception:
                pass


def _cfg(workspace_id) -> WorkspaceAIConfig | None:
    return (
        WorkspaceAIConfig.objects.filter(workspace_id=workspace_id, enabled=True)
        .only("anthropic_key", "openai_key", "chat_model", "embed_model")
        .first()
    )


def _route(event_type: str) -> list[Callable]:
    """Map event type → list of agent dispatchers (each takes Event,
    cfg). Order matters: MONITOR runs before ESCALATOR can act on its
    risks."""
    routes: dict[str, list[Callable]] = {
        ev.ISSUE_CREATED: [_dispatch_executor],
        ev.ISSUE_UPDATED: [_dispatch_monitor],
        ev.ISSUE_COMPLETED: [_dispatch_velocity],
        ev.ISSUE_BLOCKED: [_dispatch_monitor],
        ev.GOAL_CREATED: [],   # PLANNER is triggered explicitly from the API
        ev.GOAL_SCAN: [_dispatch_monitor, _dispatch_communicator],
        ev.WEEKLY_TICK: [_dispatch_communicator, _dispatch_analyst],
    }
    return routes.get(event_type, [])


# ---- dispatchers --------------------------------------------------------


def _dispatch_executor(event: ev.Event, cfg: WorkspaceAIConfig | None) -> dict:
    if not event.issue_id:
        return {"skipped": "no_issue"}
    return executor.suggest_assignee_for(event.issue_id)


def _dispatch_monitor(event: ev.Event, cfg: WorkspaceAIConfig | None) -> dict:
    if not event.project_id:
        return {"skipped": "no_project"}
    out = monitor.scan_project(event.workspace_id, event.project_id)
    # Fan-out: if any critical risks, escalate.
    critical_ids = []
    from .monitor import PredictedRisk
    if out.get("risk_ids"):
        critical_ids = list(
            PredictedRisk.objects.filter(
                id__in=out["risk_ids"], impact=PredictedRisk.IMPACT_CRITICAL,
                escalated_at__isnull=True, resolved=False,
            ).values_list("id", flat=True)
        )
    if critical_ids:
        out["escalation"] = escalator.escalate_critical_risks(critical_ids, cfg=cfg)
    return out


def _dispatch_velocity(event: ev.Event, cfg: WorkspaceAIConfig | None) -> dict:
    if not event.issue_id:
        return {"skipped": "no_issue"}
    from django.apps import apps
    Issue = apps.get_model("db", "Issue")
    issue = Issue.objects.filter(id=event.issue_id).first()
    if issue is None:
        return {"skipped": "issue_missing"}
    row = vel.record_completion(issue)
    return {"velocity_recorded": str(row.id) if row else None}


def _dispatch_analyst(event: ev.Event, cfg: WorkspaceAIConfig | None) -> dict:
    return analyst.generate_insight(
        workspace_id=event.workspace_id,
        project_id=event.project_id,
        cfg=cfg,
        days=14,
    )


def _dispatch_communicator(event: ev.Event, cfg: WorkspaceAIConfig | None) -> dict:
    """Generate weekly report for each active goal in the workspace
    (or for the goal pointed to by ``event.goal_id``)."""
    from ai.models import ProjectGoal
    goals = ProjectGoal.objects.filter(
        workspace_id=event.workspace_id,
        status__in=(ProjectGoal.STATUS_EXECUTING, ProjectGoal.STATUS_AT_RISK),
    )
    if event.goal_id:
        goals = goals.filter(id=event.goal_id)
    reports: list[dict] = []
    for goal in goals[:50]:
        try:
            reports.append(communicator.status_report(goal=goal, cfg=cfg))
        except Exception as exc:
            logger.warning("communicator failed for goal %s: %s", goal.id, exc)
    return {"reports_generated": len(reports)}


# ---- entry point --------------------------------------------------------


def handle_event(event_dict: dict) -> dict:
    """Celery task body — accepts the wire dict, returns a summary
    dict suitable for inspection. NEVER raises (catches everything
    and records a rejection action)."""
    event = ev.Event.from_dict(event_dict)
    summary: dict[str, Any] = {
        "event": event.type,
        "workspace_id": event.workspace_id,
        "ran": [],
        "skipped": None,
    }

    # 1. Drop loop-trigger events.
    if event.modified_by_agent:
        summary["skipped"] = "modified_by_agent"
        return summary

    # 2. Dedupe — repeated delivery of the same event within 60s.
    dedupe_key = f"ai:event_seen:{event.type}:{event.issue_id or event.project_id or event.workspace_id}"
    if not cache.add(dedupe_key, "1", timeout=DEDUPE_TTL):
        summary["skipped"] = "dedupe"
        return summary

    # 3. Kill switch + breaker.
    try:
        ensure_agents_allowed(event.workspace_id)
    except AgentsHalted as exc:
        log_action(
            workspace_id=event.workspace_id,
            project_id=event.project_id,
            target_issue_id=event.issue_id,
            agent_type=AgentAction.AGENT_ORCHESTRATOR,
            action_type="escalate_to_pm",  # uses a known matrix key
            input=event.to_dict(),
            output={"halt_reason": str(exc)},
            reasoning=str(exc),
            force_status="rejected",
        )
        summary["skipped"] = f"halted:{exc}"
        return summary

    # 4. Issue-scoped lock so two events on the same issue don't race.
    with _issue_lock(event.issue_id) as acquired:
        if not acquired:
            summary["skipped"] = "lock_busy"
            return summary

        cfg = _cfg(event.workspace_id)
        agents = _route(event.type)
        if not agents:
            summary["skipped"] = "no_route"
            return summary

        # 5. Run each agent; cap by MAX_HOPS to defend against fan-out.
        hops = 0
        for agent_fn in agents:
            if hops >= MAX_HOPS:
                summary["skipped"] = "hop_cap"
                break
            try:
                out = agent_fn(event, cfg)
                summary["ran"].append(
                    {"agent": agent_fn.__name__, "result": _summarize(out)}
                )
            except AgentsHalted as exc:
                summary["ran"].append(
                    {"agent": agent_fn.__name__, "result": {"halted": str(exc)}}
                )
                break
            except Exception as exc:
                logger.exception("agent %s crashed for %s", agent_fn.__name__, event.type)
                summary["ran"].append(
                    {"agent": agent_fn.__name__, "result": {"error": f"{type(exc).__name__}: {exc}"}}
                )
            hops += 1

    log_action(
        workspace_id=event.workspace_id,
        project_id=event.project_id,
        target_issue_id=event.issue_id,
        agent_type=AgentAction.AGENT_ORCHESTRATOR,
        action_type="add_comment",  # closest matrix slot for "logging"
        input={"event": event.type, "issue_id": event.issue_id},
        output=summary,
        reasoning=f"routed {event.type} to {len(summary['ran'])} agents",
    )
    return summary


def _summarize(out: dict) -> dict:
    """Trim long lists from the result so the summary stays compact."""
    if not isinstance(out, dict):
        return {"value": str(out)[:200]}
    trimmed = {}
    for k, v in out.items():
        if isinstance(v, list) and len(v) > 10:
            trimmed[k] = v[:5] + [f"... +{len(v)-5}"]
        elif isinstance(v, str) and len(v) > 300:
            trimmed[k] = v[:300] + "..."
        else:
            trimmed[k] = v
    return trimmed
