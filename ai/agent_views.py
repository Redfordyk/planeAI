"""TZ 5.6 — DRF views for the agent transparency UI.

Three concerns live here:

  1. **Audit feed** — :class:`AgentActionListView` and
     :class:`IssuesTouchedView` give the React UI read access to the
     append-only ``AIAgentActionLog`` rows the worker (TZ 5.2) writes.
     Both filter strictly by the caller's project memberships — a
     workspace admin who is not on a project still cannot read its
     agent actions (mirrors :func:`ai.acl.allowed_projects`).

  2. **Undo** — :class:`AgentActionUndoView` rolls back ONE class of
     action: ``set_labels``. The previous label state is recoverable
     because :func:`ai.agent_worker._apply_set_labels` snapshots the
     prior label ids into ``output.previous_label_ids`` *before* it
     calls ``issue.labels.set``. Other tools either edit irrecoverable
     data (``update_description`` — no snapshot of free-form text) or
     have their own human-decision UI (``add_comment`` — delete the
     comment in Plane's existing UI).

  3. **Agent toggle** — :class:`AgentListView` / :class:`AgentDetailView`
     surface ``AIAgent.enabled`` so an admin can pause the autonomous
     loop from the UI without revoking ProjectMember rows. Toggle is
     workspace-admin only (mirrors Plane's role rules), reads are
     workspace-member.

Pre-existing :func:`ai.views._is_workspace_member` and
:func:`ai.acl.allowed_projects` are the single ACL choke point — no
view here does its own ProjectMember query.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from django.apps import apps as django_apps
from django.db import transaction
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from ai.acl import ROLE, allowed_projects
from ai.agent_triggers import agent_acting
from ai.models import AIAgent, AIAgentActionLog
from ai.views import _is_workspace_member


logger = logging.getLogger("plane.ai.agent_views")


# Page size cap on the feed. UIs can scroll; we paginate. Hard ceiling
# protects the DB from a sneaky ?page_size=100000 — anyone wanting
# raw export can use the Django admin.
MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 30


# Set of tool names whose effect we know how to roll back. Anything
# else returns 422 from the undo endpoint. Driven by what the
# corresponding ``apply_*`` handler stores in ``output``:
#
#   - ``set_labels`` writes ``previous_label_ids`` (TZ 5.6) — we can
#     restore the prior membership exactly.
#
# Other tools are intentionally NOT here: ``update_description``
# would need a description snapshot we don't take (description is
# free-form user text that may have been edited since), and
# ``add_comment`` / ``suggest_assignee`` already have a "delete
# comment" affordance in Plane's UI.
REVERSIBLE_TOOLS: frozenset[str] = frozenset({"set_labels"})


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


def _serialize_action(action: AIAgentActionLog) -> dict:
    """Shape the audit-log row the UI consumes.

    Keeps the API narrow: ``input`` / ``output`` are passed through as
    JSON (the UI knows how to render each tool's flavour), and we
    surface the derived "is this reversible right now?" flag so the
    feed doesn't need to re-implement the rule. ``rationale`` is the
    short human-readable label the UI shows in the feed — derived
    from ``tool_name`` so the React component doesn't ship a Russian
    glossary.
    """
    reversible = (
        action.tool_name in REVERSIBLE_TOOLS
        and action.status == AIAgentActionLog.STATUS_APPLIED
        and action.undone_at is None
    )
    return {
        "id": str(action.id),
        "agent_id": str(action.agent_id),
        "workspace_id": str(action.workspace_id),
        "project_id": str(action.project_id),
        "issue_id": str(action.issue_id),
        "tool_name": action.tool_name,
        "status": action.status,
        "input": action.input,
        "output": action.output,
        "error": action.error,
        "rationale": _action_rationale(action),
        "created_at": action.created_at.isoformat(),
        "undone_at": action.undone_at.isoformat() if action.undone_at else None,
        "undone_by_id": str(action.undone_by_id) if action.undone_by_id else None,
        "reversible": reversible,
    }


def _action_rationale(action: AIAgentActionLog) -> str:
    """Single short label the UI uses in the feed row.

    Centralised here (not in React) so the wording stays consistent
    with what the audit-log model means by each tool, and so a future
    scenario adding a new tool only touches this server-side helper.
    """
    tool = action.tool_name
    if tool == "set_priority":
        return f"приоритет → {action.input.get('priority', '?')}"
    if tool == "set_labels":
        labels = action.input.get("labels") or []
        return "метки: " + (", ".join(map(str, labels[:5])) if labels else "—")
    if tool == "suggest_assignee":
        return f"предложил исполнителя: {action.input.get('user_email', '?')}"
    if tool == "add_comment":
        text = (action.input.get("text") or "").strip()
        return text[:120] + ("…" if len(text) > 120 else "")
    if tool == "update_description":
        return "описание обновлено"
    if tool == "find_work_items":
        return f"поиск похожих: {action.input.get('query', '')[:80]}"
    return tool


# ---------------------------------------------------------------------------
# Audit feed
# ---------------------------------------------------------------------------


class AgentActionListView(APIView):
    """``GET /api/ai/workspaces/<workspace_id>/agent/actions/``.

    Returns the agent's actions in the workspace, filtered down to
    the projects the caller is a member of. Query parameters:

      - ``project`` (UUID, optional) — narrow to one project.
        Rejected with 403 if the caller isn't a member there.
      - ``tool`` (str, optional) — filter by ``tool_name``.
      - ``status`` (str, optional) — ``applied`` / ``rejected`` /
        ``error``.
      - ``issue`` (UUID, optional) — narrow to one issue.
      - ``since`` (ISO-8601, optional) — only actions newer than this.
      - ``page`` (int, default 1), ``page_size`` (int, default 30,
        capped at :data:`MAX_PAGE_SIZE`).

    Order: newest first (``-created_at``), index-backed
    (``ai_agent_log_ws_idx``).
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, workspace_id):
        user = request.user
        if not _is_workspace_member(user, workspace_id):
            return Response(
                {"error": "not a workspace member"},
                status=status.HTTP_403_FORBIDDEN,
            )

        allowed = allowed_projects(user, workspace_id)
        if not allowed:
            # Workspace member but on no projects — nothing visible.
            # Empty page is a better UX than 403; the feed renders
            # "you haven't been added to any projects yet" rather
            # than looking broken.
            return Response(
                {"results": [], "count": 0, "page": 1, "page_size": DEFAULT_PAGE_SIZE}
            )

        qs = AIAgentActionLog.objects.filter(
            workspace_id=workspace_id, project_id__in=allowed
        )

        project_filter = request.query_params.get("project")
        if project_filter:
            if str(project_filter) not in {str(p) for p in allowed}:
                return Response(
                    {"error": "not a member of this project"},
                    status=status.HTTP_403_FORBIDDEN,
                )
            qs = qs.filter(project_id=project_filter)

        tool = request.query_params.get("tool")
        if tool:
            qs = qs.filter(tool_name=tool)

        status_filter = request.query_params.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter)

        issue_filter = request.query_params.get("issue")
        if issue_filter:
            qs = qs.filter(issue_id=issue_filter)

        since = request.query_params.get("since")
        if since:
            try:
                parsed = datetime.fromisoformat(since.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                qs = qs.filter(created_at__gte=parsed)
            except ValueError:
                return Response(
                    {"error": "invalid 'since' (expect ISO-8601)"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        page = max(int(request.query_params.get("page") or 1), 1)
        page_size = min(
            max(int(request.query_params.get("page_size") or DEFAULT_PAGE_SIZE), 1),
            MAX_PAGE_SIZE,
        )

        total = qs.count()
        rows = list(
            qs.order_by("-created_at")
            .select_related("undone_by")
            [(page - 1) * page_size : page * page_size]
        )

        return Response(
            {
                "results": [_serialize_action(a) for a in rows],
                "count": total,
                "page": page,
                "page_size": page_size,
            }
        )


# ---------------------------------------------------------------------------
# Undo
# ---------------------------------------------------------------------------


class AgentActionUndoView(APIView):
    """``POST /api/ai/workspaces/<workspace_id>/agent/actions/<action_id>/undo/``.

    Rolls back a previously-applied reversible action.

    Refused (422 / 409) when:
      - the action's tool is not in :data:`REVERSIBLE_TOOLS`,
      - the action is not ``status=applied`` (we don't undo rejections
        — there's nothing to undo),
      - the action has already been undone (``undone_at IS NOT NULL``),
      - the snapshot in ``output`` is missing (defensive: would mean
        the action was written by an older worker).

    Refused (403) when the caller isn't a member of the action's
    project. The action belongs to a project; the same per-project
    visibility rule that gates the feed gates the undo.

    The undo wraps the Plane write in :func:`agent_acting` so the
    post_save signal recognises this as agent-domain housekeeping
    and does NOT re-enqueue the agent (otherwise the agent would
    immediately reset the labels we just restored).
    """

    permission_classes = [IsAuthenticated]

    def post(self, request, workspace_id, action_id):
        user = request.user
        if not _is_workspace_member(user, workspace_id):
            return Response(
                {"error": "not a workspace member"},
                status=status.HTTP_403_FORBIDDEN,
            )

        try:
            action = AIAgentActionLog.objects.select_related("agent").get(
                id=action_id, workspace_id=workspace_id
            )
        except AIAgentActionLog.DoesNotExist:
            return Response(
                {"error": "action not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        allowed = {str(p) for p in allowed_projects(user, workspace_id)}
        if str(action.project_id) not in allowed:
            return Response(
                {"error": "not a member of the action's project"},
                status=status.HTTP_403_FORBIDDEN,
            )

        if action.tool_name not in REVERSIBLE_TOOLS:
            return Response(
                {"error": f"action {action.tool_name!r} is not reversible"},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        if action.status != AIAgentActionLog.STATUS_APPLIED:
            return Response(
                {"error": "only applied actions can be undone"},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        if action.undone_at is not None:
            return Response(
                {"error": "action already undone"},
                status=status.HTTP_409_CONFLICT,
            )

        try:
            self._undo_dispatch(action)
        except _UndoError as exc:
            return Response({"error": str(exc)}, status=exc.http_status)

        # Atomic stamp: any two clients racing here will only see one
        # winner — ``update`` returns the row count.
        with transaction.atomic():
            now = datetime.now(timezone.utc)
            updated = AIAgentActionLog.objects.filter(
                id=action.id, undone_at__isnull=True
            ).update(undone_at=now, undone_by=user)
            if not updated:
                # A concurrent undo beat us. The Plane write above is
                # idempotent (restoring the same labels twice is a
                # no-op) so we just report the race outcome.
                action.refresh_from_db()
                return Response(
                    {"error": "action already undone"},
                    status=status.HTTP_409_CONFLICT,
                )

        action.refresh_from_db()
        return Response(_serialize_action(action))

    # ------------------------------------------------------------------
    # Per-tool undo handlers
    # ------------------------------------------------------------------

    def _undo_dispatch(self, action: AIAgentActionLog) -> None:
        """Dispatch the reversal by tool name. Raises :class:`_UndoError`
        on data corruption (missing snapshot, deleted issue)."""
        if action.tool_name == "set_labels":
            return self._undo_set_labels(action)
        # Defensive — REVERSIBLE_TOOLS check above should have caught
        # this. We re-raise rather than silently no-op so the test
        # suite catches a future drift between REVERSIBLE_TOOLS and
        # this dispatcher.
        raise _UndoError(
            f"no undo handler for {action.tool_name!r}",
            http_status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    def _undo_set_labels(self, action: AIAgentActionLog) -> None:
        Issue = django_apps.get_model("db", "Issue")
        Label = django_apps.get_model("db", "Label")
        prev_ids = action.output.get("previous_label_ids")
        if prev_ids is None:
            raise _UndoError(
                "snapshot missing — action was written by an older worker"
            )
        issue = (
            Issue.objects.filter(id=action.issue_id, deleted_at__isnull=True)
            .first()
        )
        if issue is None:
            raise _UndoError("target issue no longer exists",
                             http_status=status.HTTP_410_GONE)
        # Restrict to labels still in the project (a label deleted
        # since the action ran is silently dropped; we restore as much
        # as we can — the alternative is failing the undo entirely,
        # which is worse for the operator).
        label_qs = Label.objects.filter(
            id__in=prev_ids, project_id=action.project_id
        )
        with agent_acting(issue.id):
            issue.labels.set(
                label_qs,
                through_defaults={
                    "workspace_id": issue.workspace_id,
                    "project_id": issue.project_id,
                },
            )


class _UndoError(Exception):
    """Raised inside the undo dispatcher to surface a user-visible
    failure with a chosen HTTP status. Caught in :meth:`post` so we
    never propagate an unhandled exception past the API boundary."""

    def __init__(self, message: str, http_status: int = status.HTTP_422_UNPROCESSABLE_ENTITY):
        super().__init__(message)
        self.http_status = http_status


# ---------------------------------------------------------------------------
# Agent toggle
# ---------------------------------------------------------------------------


def _is_workspace_admin(user, workspace_id) -> bool:
    """True if ``user`` is an active workspace admin of
    ``workspace_id``. Mirrors :class:`ai.acl.ROLE.ADMIN`."""
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    WorkspaceMember = django_apps.get_model("db", "WorkspaceMember")
    return WorkspaceMember.objects.filter(
        member=user,
        workspace_id=workspace_id,
        role=ROLE.ADMIN.value,
        is_active=True,
        deleted_at__isnull=True,
    ).exists()


def _serialize_agent(agent: AIAgent) -> dict:
    return {
        "id": str(agent.id),
        "workspace_id": str(agent.workspace_id),
        "user_id": str(agent.user_id),
        "user_email": getattr(agent.user, "email", "") or "",
        "enabled": agent.enabled,
        "created_at": agent.created_at.isoformat(),
        "updated_at": agent.updated_at.isoformat(),
    }


class AgentListView(APIView):
    """``GET /api/ai/workspaces/<workspace_id>/agents/``.

    Lists the workspace's AI agents (``AIAgent`` rows). Read-only,
    open to any workspace member — the UI uses this to render the
    "agent: on / off" badge in the activity feed page, regardless of
    role. Sensitive details (encrypted keys etc.) are not on the
    ``AIAgent`` model itself, so we don't expose anything privileged.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, workspace_id):
        if not _is_workspace_member(request.user, workspace_id):
            return Response(
                {"error": "not a workspace member"},
                status=status.HTTP_403_FORBIDDEN,
            )
        rows = list(
            AIAgent.objects.filter(workspace_id=workspace_id).select_related("user")
        )
        return Response({"results": [_serialize_agent(a) for a in rows]})


class AgentDetailView(APIView):
    """``PATCH /api/ai/workspaces/<workspace_id>/agents/<agent_id>/``.

    Toggle :attr:`AIAgent.enabled`. Admin-only — flipping the agent
    off / on is a workspace-level capability, same authority bar as
    revoking a member.

    Body: ``{"enabled": true|false}``. Anything else is rejected.
    """

    permission_classes = [IsAuthenticated]

    def patch(self, request, workspace_id, agent_id):
        if not _is_workspace_admin(request.user, workspace_id):
            return Response(
                {"error": "workspace admin required"},
                status=status.HTTP_403_FORBIDDEN,
            )
        try:
            agent = AIAgent.objects.select_related("user").get(
                id=agent_id, workspace_id=workspace_id
            )
        except AIAgent.DoesNotExist:
            return Response(
                {"error": "agent not found"},
                status=status.HTTP_404_NOT_FOUND,
            )
        if "enabled" not in request.data:
            return Response(
                {"error": "missing 'enabled' boolean"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        enabled = request.data["enabled"]
        if not isinstance(enabled, bool):
            return Response(
                {"error": "'enabled' must be a boolean"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if agent.enabled != enabled:
            agent.enabled = enabled
            agent.save(update_fields=["enabled", "updated_at"])
            logger.info(
                "agent %s %s by user=%s",
                agent.id,
                "enabled" if enabled else "disabled",
                request.user.id,
            )
        return Response(_serialize_agent(agent))


# ---------------------------------------------------------------------------
# Touched-by-agent badge
# ---------------------------------------------------------------------------


class IssuesTouchedView(APIView):
    """``GET /api/ai/workspaces/<workspace_id>/issues/touched/?ids=...``.

    Bulk lookup powering the "🤖 действие ИИ" badge on issue cards.
    Avoids N+1 — the issue list passes its currently-rendered page of
    ids in one comma-separated query string, gets back a flat map of
    ``{issue_id: bool}`` and decorates locally. Caller may pass at
    most :data:`MAX_TOUCHED_QUERY` ids per request — anything beyond
    means the issue list itself is paginating wrong.

    "Touched" means there exists at least one ``status=applied``,
    not-undone audit row for the issue. Rejected / errored / undone
    actions don't count — they didn't change anything visible.

    ACL: per-issue. The view filters the input ids through the
    caller's allowed_projects so the badge map never reveals "you
    can't see this project but the agent did something on its
    issue".
    """

    permission_classes = [IsAuthenticated]

    MAX_TOUCHED_QUERY = 200

    def get(self, request, workspace_id):
        if not _is_workspace_member(request.user, workspace_id):
            return Response(
                {"error": "not a workspace member"},
                status=status.HTTP_403_FORBIDDEN,
            )
        raw = (request.query_params.get("ids") or "").strip()
        if not raw:
            return Response({"touched": {}})
        ids = [s.strip() for s in raw.split(",") if s.strip()]
        if len(ids) > self.MAX_TOUCHED_QUERY:
            return Response(
                {"error": f"at most {self.MAX_TOUCHED_QUERY} ids per request"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        allowed = allowed_projects(request.user, workspace_id)
        if not allowed:
            return Response({"touched": {i: False for i in ids}})

        touched_ids = set(
            AIAgentActionLog.objects.filter(
                workspace_id=workspace_id,
                project_id__in=allowed,
                issue_id__in=ids,
                status=AIAgentActionLog.STATUS_APPLIED,
                undone_at__isnull=True,
            )
            .values_list("issue_id", flat=True)
            .distinct()
        )
        touched_str = {str(i) for i in touched_ids}
        return Response({"touched": {i: i in touched_str for i in ids}})


__all__ = [
    "AgentActionListView",
    "AgentActionUndoView",
    "AgentListView",
    "AgentDetailView",
    "IssuesTouchedView",
    "REVERSIBLE_TOOLS",
]
