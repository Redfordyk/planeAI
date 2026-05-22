"""Smoke-verify ai.acl helpers against synthetic Plane fixtures.

Install: docker cp scripts/verify_acl.py \
    plane-ce-api-1:/code/plane/db/management/commands/verify_acl.py

Run:     docker compose exec api python manage.py verify_acl

Creates a throwaway workspace + project + five users + one issue,
exercises every interesting branch of `allowed_projects` and
`filter_ids_by_acl`, then deletes the fixtures it created.

Exit code 0 = all assertions passed; non-zero = at least one failed
case (the offending line is printed).
"""

from __future__ import annotations

import uuid

from django.core.management.base import BaseCommand
from django.db import transaction

from ai.acl import ROLE, allowed_projects, filter_ids_by_acl
from plane.db.models import (
    Issue,
    Project,
    ProjectMember,
    User,
    Workspace,
    WorkspaceMember,
)


SLUG = f"acl-smoke-{uuid.uuid4().hex[:8]}"


def make_user(handle: str) -> User:
    return User.objects.create(
        email=f"{handle}+{uuid.uuid4().hex[:6]}@example.test",
        username=f"{handle}-{uuid.uuid4().hex[:6]}",
        first_name=handle,
        is_password_autoset=True,
    )


class Command(BaseCommand):
    help = "Smoke-verify ai.acl.allowed_projects and filter_ids_by_acl."

    @transaction.atomic
    def handle(self, *args, **opts):
        # ---- fixtures -------------------------------------------------
        owner = make_user("owner")
        admin = make_user("admin")
        member = make_user("member")
        guest = make_user("guest")
        outsider = make_user("outsider")
        ws_admin_only = make_user("wsadminonly")

        ws = Workspace.objects.create(name="ACL Smoke WS", slug=SLUG, owner=owner)
        # Project ws membership for everyone except outsider; owner=admin
        WorkspaceMember.objects.bulk_create([
            WorkspaceMember(workspace=ws, member=owner, role=ROLE.ADMIN.value, is_active=True),
            WorkspaceMember(workspace=ws, member=admin, role=ROLE.ADMIN.value, is_active=True),
            WorkspaceMember(workspace=ws, member=member, role=ROLE.MEMBER.value, is_active=True),
            WorkspaceMember(workspace=ws, member=guest, role=ROLE.GUEST.value, is_active=True),
            WorkspaceMember(workspace=ws, member=ws_admin_only, role=ROLE.ADMIN.value, is_active=True),
        ])

        prj = Project.objects.create(
            workspace=ws,
            name="ACL Smoke PRJ",
            identifier="ACL",
            created_by=owner,
        )

        # ProjectMember rows: admin=ADMIN, member=MEMBER, guest=GUEST.
        # ws_admin_only intentionally has NO ProjectMember row, then
        # below we'll also create a *guest* PM row for them so the
        # workspace-admin escape hatch actually triggers (Plane requires
        # both ws-admin + any PM row).
        ProjectMember.objects.bulk_create([
            ProjectMember(workspace=ws, project=prj, member=admin, role=ROLE.ADMIN.value, is_active=True),
            ProjectMember(workspace=ws, project=prj, member=member, role=ROLE.MEMBER.value, is_active=True),
            ProjectMember(workspace=ws, project=prj, member=guest, role=ROLE.GUEST.value, is_active=True),
            ProjectMember(workspace=ws, project=prj, member=ws_admin_only, role=ROLE.GUEST.value, is_active=True),
        ])

        issue = Issue.objects.create(
            workspace=ws,
            project=prj,
            name="ACL smoke target",
            created_by=owner,
        )
        ids = [str(issue.id)]

        # ---- assertions ----------------------------------------------
        failures: list[str] = []

        def check(label: str, got, want):
            if got != want:
                failures.append(f"FAIL  {label}: got={got!r}  want={want!r}")
            else:
                print(f"ok    {label}")

        # allowed_projects
        check("allowed_projects(admin)    contains prj",
              prj.id in allowed_projects(admin, ws.id), True)
        check("allowed_projects(member)   contains prj",
              prj.id in allowed_projects(member, ws.id), True)
        check("allowed_projects(guest)    contains prj (read-only ok)",
              prj.id in allowed_projects(guest, ws.id), True)
        check("allowed_projects(outsider) empty",
              allowed_projects(outsider, ws.id), [])
        check("allowed_projects(anon)     empty",
              allowed_projects(None, ws.id), [])

        # filter_ids_by_acl — write semantics
        check("write(admin)         allowed",     filter_ids_by_acl(ids, admin) == ids, True)
        check("write(member)        allowed",     filter_ids_by_acl(ids, member) == ids, True)
        check("write(guest)         denied",      filter_ids_by_acl(ids, guest), [])
        check("write(outsider)      denied",      filter_ids_by_acl(ids, outsider), [])
        check("write(ws_admin_only) allowed via escape hatch",
              filter_ids_by_acl(ids, ws_admin_only) == ids, True)
        check("write(anon)          denied",      filter_ids_by_acl(ids, None), [])
        check("write([])            empty",       filter_ids_by_acl([], admin), [])

        # Deactivate the member's row -> should lose write access.
        ProjectMember.objects.filter(project=prj, member=member).update(is_active=False)
        check("write(inactive member) denied",    filter_ids_by_acl(ids, member), [])

        # ---- cleanup --------------------------------------------------
        # Atomic block will roll back automatically on raise. Force a
        # rollback so the smoke run leaves NO trace in the DB.
        transaction.set_rollback(True)

        if failures:
            for f in failures:
                self.stdout.write(self.style.ERROR(f))
            raise SystemExit(1)

        self.stdout.write(self.style.SUCCESS("\nALL ACL ASSERTIONS PASSED"))
