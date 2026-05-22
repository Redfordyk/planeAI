"""ACL helpers for AI features.

Plane's permission model (verified against plane.utils.permissions.base.ROLE
and the `allow_permission` decorator in v1.3.1):

  ROLE.ADMIN  = 20  -- workspace/project admin
  ROLE.MEMBER = 15  -- can read AND write issues/comments/pages
  ROLE.GUEST  = 5   -- read-only for most write paths

Write access on an issue (the bulk-update use case) in upstream Plane is
gated by ``[ROLE.ADMIN, ROLE.MEMBER]`` at the project level, with one
escape hatch implemented in ``allow_permission``: a workspace admin who
is *any kind* of active ProjectMember of the target project is allowed
through. We mirror both rules here so our ACL filter never grants more
than Plane itself would, and never grants less.

These two functions are the only authorised choke points for AI-driven
reads and writes — every retrieval filter and every bulk operation must
go through them. They take a Django ``User`` instance, not an id, so the
caller cannot accidentally pass a stale or fabricated id.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Iterable
from uuid import UUID

from django.db.models import Q


class ROLE(IntEnum):
    """Mirrors plane.utils.permissions.base.ROLE.

    Defined here so we never have to import from upstream's `utils`
    package and inherit its import surface. Values are authoritative
    in the database (`role` is a PositiveSmallIntegerField with these
    choices on both ProjectMember and WorkspaceMember).
    """

    ADMIN = 20
    MEMBER = 15
    GUEST = 5


WRITE_PROJECT_ROLES: tuple[int, ...] = (ROLE.ADMIN.value, ROLE.MEMBER.value)


def allowed_projects(user, workspace_id: UUID | str) -> list[UUID]:
    """Project ids of `workspace_id` that `user` may READ.

    Any active ``ProjectMember`` row grants read access (Plane treats
    guests as readers for most issue views). Soft-deleted memberships
    (``deleted_at IS NOT NULL``) and inactive ones are excluded.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return []

    from plane.db.models import ProjectMember

    return list(
        ProjectMember.objects.filter(
            member=user,
            workspace_id=workspace_id,
            is_active=True,
            deleted_at__isnull=True,
        ).values_list("project_id", flat=True)
    )


def filter_ids_by_acl(work_item_ids: Iterable[UUID | str], user) -> list[str]:
    """Return the subset of `work_item_ids` `user` is allowed to WRITE.

    Write semantics match Plane's ``allow_permission`` decorator:

      1. Direct path — user has an active ``ProjectMember`` row on the
         issue's project with ``role IN (MEMBER, ADMIN)``.
      2. Workspace-admin escape — user is an active workspace admin of
         the issue's workspace AND has *any* active ``ProjectMember``
         row on that project (including guest).

    Anything not produced by these two queries is filtered out, so a
    guest-only project member, an inactive member, or a stranger to the
    project gets nothing back. Order of the input is not preserved;
    callers that care should re-sort.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return []

    ids = [str(i) for i in work_item_ids]
    if not ids:
        return []

    from plane.db.models import Issue, WorkspaceMember

    # 1. Direct write — role >= MEMBER on the issue's project.
    direct_writable = set(
        Issue.objects.filter(
            id__in=ids,
            project__project_projectmember__member=user,
            project__project_projectmember__is_active=True,
            project__project_projectmember__deleted_at__isnull=True,
            project__project_projectmember__role__in=WRITE_PROJECT_ROLES,
            deleted_at__isnull=True,
        ).values_list("id", flat=True)
    )

    # 2. Workspace-admin escape — any active project membership counts
    #    if the user is an active admin of that workspace.
    ws_admin_workspaces = list(
        WorkspaceMember.objects.filter(
            member=user,
            role=ROLE.ADMIN.value,
            is_active=True,
            deleted_at__isnull=True,
        ).values_list("workspace_id", flat=True)
    )

    escape_writable: set = set()
    if ws_admin_workspaces:
        remaining = [i for i in ids if i not in {str(x) for x in direct_writable}]
        if remaining:
            escape_writable = set(
                Issue.objects.filter(
                    Q(id__in=remaining)
                    & Q(workspace_id__in=ws_admin_workspaces)
                    & Q(project__project_projectmember__member=user)
                    & Q(project__project_projectmember__is_active=True)
                    & Q(project__project_projectmember__deleted_at__isnull=True),
                    deleted_at__isnull=True,
                ).values_list("id", flat=True)
            )

    return [str(i) for i in (direct_writable | escape_writable)]
