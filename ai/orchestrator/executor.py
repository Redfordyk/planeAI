"""EXECUTOR agent (TZ 9.1).

Assigns/proposes assignees for issues with no current owner, scoring
candidates by current open-issue load (lowest wins). The Decision
Layer demotes auto-assignment to NOTIFY (people-affecting), so by
default the EXECUTOR posts a suggestion comment rather than directly
writing IssueAssignee — same pattern as the single-issue agent
worker's ``suggest_assignee``.

This module is intentionally small: real assignment / rebalancing
involves people politics (skills, vacation, partial-FTE) we don't
model. The proposal is "lowest-load active project member"; the
human approves or overrides.
"""

from __future__ import annotations

import logging
from typing import Optional
from uuid import UUID

from django.apps import apps
from django.db.models import Count, Q

from ai.acl import ROLE
from ai.models import AgentAction
from .base import log_action
from .breaker import ensure_agents_allowed
import html


logger = logging.getLogger("plane.ai.orchestrator.executor")

AGENT = AgentAction.AGENT_EXECUTOR


def _candidate_user_loads(project_id) -> list[tuple]:
    """Returns ``[(user_id, open_issue_count, email), ...]`` for
    active project members, sorted by load ascending."""
    ProjectMember = apps.get_model("db", "ProjectMember")
    Issue = apps.get_model("db", "Issue")

    members = list(
        ProjectMember.objects.filter(
            project_id=project_id,
            is_active=True,
            deleted_at__isnull=True,
            role__in=(ROLE.MEMBER.value, ROLE.ADMIN.value),
        ).values("member_id", "member__email")
    )
    if not members:
        return []
    user_ids = [m["member_id"] for m in members]
    load_qs = (
        Issue.objects.filter(
            project_id=project_id,
            assignees__id__in=user_ids,
            deleted_at__isnull=True,
        )
        .exclude(state__group__in=("completed", "cancelled"))
        .values("assignees__id")
        .annotate(n=Count("id"))
    )
    load_by_user = {r["assignees__id"]: r["n"] for r in load_qs}
    rows = [
        (m["member_id"], load_by_user.get(m["member_id"], 0), m["member__email"])
        for m in members
    ]
    rows.sort(key=lambda r: r[1])
    return rows


def suggest_assignee_for(issue_id: UUID | str) -> dict:
    """Pick lowest-load member, post a suggestion comment from the
    orchestrator. Records as NOTIFY (because it concerns a person)
    so the activity feed surfaces it.
    """
    Issue = apps.get_model("db", "Issue")
    IssueComment = apps.get_model("db", "IssueComment")
    issue = (
        Issue.objects.filter(id=issue_id, deleted_at__isnull=True, is_draft=False)
        .select_related("project", "workspace")
        .first()
    )
    if issue is None:
        return {"skipped": "issue_missing"}

    ensure_agents_allowed(issue.workspace_id)

    # Skip if already assigned.
    if issue.assignees.exists():
        return {"skipped": "already_assigned"}

    candidates = _candidate_user_loads(issue.project_id)
    if not candidates:
        return {"skipped": "no_candidates"}
    user_id, load, email = candidates[0]
    reason = f"lowest open-issue load on this project ({load} open)"

    # Post the suggestion comment as the issue's created_by (we don't
    # spin up a synthetic agent user here; the proposal is on behalf
    # of the orchestrator and the audit row carries the rationale).
    actor = None
    text = f"🤖 EXECUTOR предлагает назначить @{email} — {reason}"
    comment = IssueComment.objects.create(
        project_id=issue.project_id,
        issue_id=issue.id,
        actor=actor,
        comment_stripped=text,
        comment_html=f"<p>{html.escape(text)}</p>",
        access="INTERNAL",
    )

    log_action(
        workspace_id=issue.workspace_id,
        project_id=issue.project_id,
        target_issue_id=issue.id,
        agent_type=AGENT,
        action_type="suggest_assignee",
        input={"candidates": [{"user_id": str(c[0]), "load": c[1], "email": c[2]} for c in candidates[:5]]},
        output={"suggested_user_id": str(user_id), "comment_id": str(comment.id)},
        reasoning=reason,
    )
    return {
        "suggested_user_id": str(user_id),
        "suggested_email": email,
        "load": load,
        "comment_id": str(comment.id),
    }
