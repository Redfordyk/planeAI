"""TeamVelocity recording (TZ 8.2).

When an Issue's state moves into a 'completed' group, write one
``TeamVelocity`` row. ANALYST + MONITOR consume these.

Cold start: until we have ~20 samples per (user, task_type) bucket,
ANALYST falls back to project-wide aggregates.
"""

from __future__ import annotations

import logging

from django.apps import apps
from django.utils import timezone

from ai.models import TeamVelocity


logger = logging.getLogger("plane.ai.orchestrator.velocity")


def _primary_task_type(issue) -> str:
    """First label name on the issue (alphabetical), or
    ``'uncategorised'`` if none."""
    try:
        names = list(
            issue.labels.values_list("name", flat=True).order_by("name")[:1]
        )
        return (names[0] or "uncategorised")[:60] if names else "uncategorised"
    except Exception:
        return "uncategorised"


def _first_assignee(issue):
    try:
        return issue.assignees.first()
    except Exception:
        return None


def record_completion(issue) -> TeamVelocity | None:
    """Idempotent — one row per issue. Re-completion (state bounces
    back and forward) updates the existing row's ``completed_at``."""
    Issue = apps.get_model("db", "Issue")
    if not isinstance(issue, Issue):
        return None
    user = _first_assignee(issue)
    task_type = _primary_task_type(issue)
    completed_at = timezone.now()
    estimated_hours = None
    actual_hours = None
    # Plane stores estimate points / minutes on Issue.estimate_point if
    # estimates are turned on for the project; we look at .estimate.
    raw_est = getattr(issue, "estimate_point", None)
    if raw_est is not None:
        try:
            estimated_hours = float(getattr(raw_est, "value", raw_est)) or None
        except Exception:
            estimated_hours = None
    started = getattr(issue, "start_date", None) or getattr(issue, "created_at", None)
    if started:
        try:
            delta = completed_at - started
            actual_hours = max(delta.total_seconds() / 3600.0, 0.1)
        except Exception:
            actual_hours = None

    row, _created = TeamVelocity.objects.update_or_create(
        issue_id=issue.id,
        defaults={
            "workspace_id": issue.workspace_id,
            "project_id": issue.project_id,
            "user": user,
            "task_type": task_type,
            "estimated_hours": estimated_hours,
            "actual_hours": actual_hours,
            "started_at": started,
            "completed_at": completed_at,
        },
    )
    return row
