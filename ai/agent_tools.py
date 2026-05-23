"""Tool implementations for the agent loop.

Each tool is a small function that takes a Plane ``db.User`` and the
JSON arguments produced by the LLM, performs an ACL-checked DB
operation, and returns a short dict for both the LLM (next turn
context) and the UI (action log).

Hard rules (CLAUDE.md invariants):

  - Every write checks the user's permissions via ``ai.acl`` and
    Plane's own role enum (member >= 15 to create issues, admin = 20
    to create projects). We never trust the LLM's claim that the
    user can do something — Plane is the source of truth.
  - All writes happen inside ``transaction.atomic()`` so a partial
    failure rolls back cleanly.
  - The agent operates strictly inside one workspace; tools that
    take ``project_id_or_identifier`` resolve identifiers against
    that workspace only.
"""

from __future__ import annotations

import logging
import re
import uuid as _uuid
from typing import Any

from django.apps import apps
from django.db import transaction

from ai.acl import ROLE


logger = logging.getLogger("plane.ai.agent.tools")


class ToolError(Exception):
    """Caller-visible error. Message goes back to the LLM."""


# ---------- helpers ---------------------------------------------------------


def _is_admin(user, workspace_id) -> bool:
    WorkspaceMember = apps.get_model("db", "WorkspaceMember")
    return WorkspaceMember.objects.filter(
        member=user,
        workspace_id=workspace_id,
        role=ROLE.ADMIN.value,
        is_active=True,
        deleted_at__isnull=True,
    ).exists()


def _is_member(user, workspace_id) -> bool:
    WorkspaceMember = apps.get_model("db", "WorkspaceMember")
    return WorkspaceMember.objects.filter(
        member=user,
        workspace_id=workspace_id,
        is_active=True,
        deleted_at__isnull=True,
    ).exists()


def _resolve_project(workspace_id, ident: str):
    """Accept a UUID, an identifier (project.identifier — like 'PRJ'),
    or a name (case-insensitive). Returns ``db.Project`` or raises."""
    Project = apps.get_model("db", "Project")
    qs = Project.objects.filter(
        workspace_id=workspace_id, deleted_at__isnull=True
    )
    # UUID
    try:
        u = _uuid.UUID(str(ident))
        prj = qs.filter(id=u).first()
        if prj:
            return prj
    except (ValueError, TypeError):
        pass
    # identifier (case-insensitive exact)
    prj = qs.filter(identifier__iexact=str(ident).strip()).first()
    if prj:
        return prj
    # name (case-insensitive contains)
    prj = qs.filter(name__iexact=str(ident).strip()).first()
    if prj:
        return prj
    raise ToolError(f"Project '{ident}' not found in this workspace")


def _resolve_member(workspace_id, hint: str):
    """Resolve an email / display_name / username to ``db.User`` that
    is an active member of the workspace."""
    User = apps.get_model("db", "User")
    WorkspaceMember = apps.get_model("db", "WorkspaceMember")
    member_ids = WorkspaceMember.objects.filter(
        workspace_id=workspace_id, is_active=True, deleted_at__isnull=True
    ).values_list("member_id", flat=True)
    qs = User.objects.filter(id__in=member_ids)
    hint = (hint or "").strip()
    if not hint:
        raise ToolError("empty member hint")
    user = qs.filter(email__iexact=hint).first()
    if user:
        return user
    user = qs.filter(username__iexact=hint).first()
    if user:
        return user
    user = qs.filter(display_name__iexact=hint).first()
    if user:
        return user
    # last-ditch contains
    user = qs.filter(email__icontains=hint).first() or qs.filter(
        display_name__icontains=hint
    ).first()
    if user:
        return user
    raise ToolError(f"Workspace member '{hint}' not found")


_IDENTIFIER_RE = re.compile(r"[^A-Z0-9]")


def _make_identifier(name: str) -> str:
    """Derive a 3-5-char project identifier from a name (Plane
    requires uppercase, unique-per-workspace)."""
    upper = _IDENTIFIER_RE.sub("", name.upper())
    if len(upper) < 3:
        upper = (upper + "PROJ")[:3]
    return upper[:5]


# ---------- tools (called from agent loop) ----------------------------------


def list_projects(user, workspace_id, _args: dict[str, Any]) -> dict[str, Any]:
    Project = apps.get_model("db", "Project")
    from ai.acl import allowed_projects

    ids = set(allowed_projects(user, workspace_id))
    rows = list(
        Project.objects.filter(id__in=ids, deleted_at__isnull=True)
        .order_by("name")
        .values("id", "identifier", "name")[:200]
    )
    return {
        "projects": [
            {"id": str(r["id"]), "identifier": r["identifier"], "name": r["name"]}
            for r in rows
        ],
    }


def list_members(user, workspace_id, _args: dict[str, Any]) -> dict[str, Any]:
    if not _is_member(user, workspace_id):
        raise ToolError("you are not a member of this workspace")
    WorkspaceMember = apps.get_model("db", "WorkspaceMember")
    rows = list(
        WorkspaceMember.objects.filter(
            workspace_id=workspace_id,
            is_active=True,
            deleted_at__isnull=True,
        )
        .select_related("member")
        .values(
            "member__id",
            "member__email",
            "member__display_name",
            "member__username",
            "role",
        )[:200]
    )
    return {
        "members": [
            {
                "id": str(r["member__id"]),
                "email": r["member__email"],
                "display_name": r["member__display_name"] or r["member__username"],
                "role": r["role"],
            }
            for r in rows
        ],
    }


def create_project(user, workspace_id, args: dict[str, Any]) -> dict[str, Any]:
    name = (args.get("name") or "").strip()
    if not name:
        raise ToolError("create_project: 'name' is required")
    if not _is_admin(user, workspace_id):
        raise ToolError(
            "create_project: only workspace admins can create projects"
        )

    Project = apps.get_model("db", "Project")
    ProjectMember = apps.get_model("db", "ProjectMember")

    identifier = (args.get("identifier") or _make_identifier(name)).upper()[:5]
    # ensure unique within workspace
    base = identifier
    n = 1
    while Project.objects.filter(
        workspace_id=workspace_id, identifier=identifier
    ).exists():
        n += 1
        identifier = f"{base[:4]}{n}"
        if n > 99:
            raise ToolError("could not derive a unique identifier")

    with transaction.atomic():
        prj = Project.objects.create(
            workspace_id=workspace_id,
            name=name[:255],
            identifier=identifier,
            description=(args.get("description") or "")[:1000],
            created_by=user,
            project_lead=user,
        )
        # add the creator as admin so they immediately see it
        ProjectMember.objects.update_or_create(
            workspace_id=workspace_id,
            project=prj,
            member=user,
            defaults={"role": ROLE.ADMIN.value, "is_active": True},
        )

    logger.info("agent.create_project: ws=%s user=%s -> %s", workspace_id, user.id, prj.id)
    return {
        "project_id": str(prj.id),
        "identifier": prj.identifier,
        "name": prj.name,
    }


def create_issue(user, workspace_id, args: dict[str, Any]) -> dict[str, Any]:
    name = (args.get("name") or "").strip()
    project_ref = args.get("project") or args.get("project_id") or args.get("identifier")
    if not name:
        raise ToolError("create_issue: 'name' is required")
    if not project_ref:
        raise ToolError("create_issue: 'project' (id/identifier/name) is required")

    project = _resolve_project(workspace_id, project_ref)

    # ACL: caller must be a member of THIS project with write access
    # (mirrors filter_ids_by_acl write semantics: role >= MEMBER, or
    # workspace-admin with any project membership).
    ProjectMember = apps.get_model("db", "ProjectMember")
    has_write = ProjectMember.objects.filter(
        member=user,
        project=project,
        is_active=True,
        deleted_at__isnull=True,
        role__in=(ROLE.MEMBER.value, ROLE.ADMIN.value),
    ).exists()
    if not has_write:
        is_ws_admin = _is_admin(user, workspace_id)
        any_pm = ProjectMember.objects.filter(
            member=user, project=project, is_active=True, deleted_at__isnull=True
        ).exists()
        if not (is_ws_admin and any_pm):
            raise ToolError(
                "create_issue: you do not have write access in this project"
            )

    Issue = apps.get_model("db", "Issue")

    priority = (args.get("priority") or "none").lower()
    if priority not in {"none", "low", "medium", "high", "urgent"}:
        priority = "none"

    assignee = None
    assignee_hint = args.get("assignee")
    if assignee_hint:
        try:
            assignee = _resolve_member(workspace_id, str(assignee_hint))
        except ToolError as e:
            # Don't fail the whole issue creation; the LLM should
            # know and try a different name on the next turn.
            assignee = None
            logger.info("agent.create_issue: assignee lookup failed: %s", e)

    with transaction.atomic():
        issue = Issue.objects.create(
            workspace_id=workspace_id,
            project=project,
            name=name[:255],
            description_stripped=(args.get("description") or "")[:5000],
            priority=priority,
            created_by=user,
        )
        if assignee is not None:
            IssueAssignee = apps.get_model("db", "IssueAssignee")
            IssueAssignee.objects.create(
                workspace_id=workspace_id,
                project=project,
                issue=issue,
                assignee=assignee,
                created_by=user,
            )

    logger.info(
        "agent.create_issue: ws=%s project=%s user=%s -> %s",
        workspace_id, project.id, user.id, issue.id,
    )
    return {
        "issue_id": str(issue.id),
        "project_id": str(project.id),
        "name": issue.name,
        "priority": issue.priority,
        "assignee_id": str(assignee.id) if assignee else None,
    }


# ---------- registry --------------------------------------------------------


REGISTRY = {
    "list_projects": list_projects,
    "list_members": list_members,
    "create_project": create_project,
    "create_issue": create_issue,
}


TOOL_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "list_projects",
            "description": "List projects in the current workspace the user can see. Call this first if the user mentions an existing project by name.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_members",
            "description": "List active members of the current workspace with their display names and emails. Call this to resolve a person referenced by name into a real user.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_project",
            "description": "Create a new project in the current workspace. Requires workspace-admin role.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Human-readable project name."},
                    "identifier": {
                        "type": "string",
                        "description": "Optional 3-5 uppercase letters used as the issue-id prefix. Derived from name if absent.",
                    },
                    "description": {"type": "string", "description": "Optional short description."},
                },
                "required": ["name"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_issue",
            "description": "Create one work item (issue) in a project. Returns the issue id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": "Project identifier (e.g. 'PRJ'), project name, or project UUID.",
                    },
                    "name": {"type": "string", "description": "Issue title."},
                    "description": {"type": "string"},
                    "priority": {
                        "type": "string",
                        "enum": ["none", "low", "medium", "high", "urgent"],
                    },
                    "assignee": {
                        "type": "string",
                        "description": "Email, display name, or username of a workspace member.",
                    },
                },
                "required": ["project", "name"],
                "additionalProperties": False,
            },
        },
    },
]
