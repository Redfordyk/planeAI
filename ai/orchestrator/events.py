"""Event types flowing from Plane signals into the orchestrator.

A lightweight typed wrapper around the dict envelopes that the Celery
``orchestrator_handle_event`` task receives. The router (router.py)
takes one of these and decides which agent(s) to wake.

We don't use Pydantic — Celery already serialises through JSON, so a
plain dict + a small validator is enough. The constructor accepts
kwargs both for ergonomics in tests and so a future migration to a
typed schema doesn't reshape the call sites.

Loop protection: every event carries ``modified_by_agent``. When the
signal handler runs ``_modified_by_agent=True`` on an issue (set by
the EXECUTOR or PLANNER right before they save) the event is dropped
in the router — see ``router.handle_event``. Without this we'd loop
forever as the agent's own save triggers another event that triggers
the agent again.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# Event types
ISSUE_CREATED = "issue_created"
ISSUE_UPDATED = "issue_updated"
ISSUE_COMPLETED = "issue_completed"  # state moved to a 'completed' group
ISSUE_BLOCKED = "issue_blocked"      # blocked label / state change
GOAL_CREATED = "goal_created"        # user just stated a goal
GOAL_SCAN = "goal_scan"              # periodic risk scan tick
WEEKLY_TICK = "weekly_tick"          # Friday 18:00 cron

EVENT_TYPES = (
    ISSUE_CREATED,
    ISSUE_UPDATED,
    ISSUE_COMPLETED,
    ISSUE_BLOCKED,
    GOAL_CREATED,
    GOAL_SCAN,
    WEEKLY_TICK,
)


@dataclass
class Event:
    type: str
    workspace_id: str
    project_id: str | None = None
    issue_id: str | None = None
    goal_id: str | None = None
    changed_fields: list[str] = field(default_factory=list)
    modified_by_agent: bool = False
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "workspace_id": str(self.workspace_id) if self.workspace_id else None,
            "project_id": str(self.project_id) if self.project_id else None,
            "issue_id": str(self.issue_id) if self.issue_id else None,
            "goal_id": str(self.goal_id) if self.goal_id else None,
            "changed_fields": list(self.changed_fields),
            "modified_by_agent": bool(self.modified_by_agent),
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Event":
        return cls(
            type=d["type"],
            workspace_id=d.get("workspace_id"),
            project_id=d.get("project_id"),
            issue_id=d.get("issue_id"),
            goal_id=d.get("goal_id"),
            changed_fields=d.get("changed_fields") or [],
            modified_by_agent=bool(d.get("modified_by_agent")),
            payload=d.get("payload") or {},
        )
