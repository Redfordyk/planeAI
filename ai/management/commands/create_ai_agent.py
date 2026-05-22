"""Create an AI-agent Plane user with restricted permissions.

TZ 5.1 DoD requires the agent to be created with project-scope
membership and a non-admin role. We provision exactly that here, in
one transaction:

  1. ``db.User`` row (the agent's identity in Plane).
  2. ``ai.AIAgent`` row (marker that this user is in fact an agent).
  3. ``db.WorkspaceMember`` with role MEMBER (NOT ADMIN).
  4. ``db.ProjectMember`` rows for the explicitly listed projects,
     all with role MEMBER. The agent is invisible in every other
     project of the workspace.

Re-running with the same email is idempotent — existing memberships
are upserted to the requested state, no duplicate rows.

Example::

    python manage.py create_ai_agent \\
        --workspace-slug acme \\
        --email triage-bot@example.com \\
        --project-identifier ENG --project-identifier DESIGN
"""

from __future__ import annotations

import uuid

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from ai.acl import ROLE
from ai.models import AIAgent


class Command(BaseCommand):
    help = "Provision an AI-agent user with project-scoped, non-admin permissions."

    def add_arguments(self, parser):
        parser.add_argument("--workspace-slug", required=True)
        parser.add_argument(
            "--email",
            required=True,
            help="Email for the agent's Plane user account.",
        )
        parser.add_argument(
            "--display-name",
            default="AI Agent",
            help="First name to show in the Plane UI.",
        )
        parser.add_argument(
            "--project-identifier",
            action="append",
            default=[],
            help=(
                "Identifier of a project the agent may act in "
                "(repeatable). Without any, the agent has no project "
                "scope and will never be triggered."
            ),
        )
        parser.add_argument(
            "--disabled",
            action="store_true",
            help="Create the agent with enabled=False.",
        )

    def handle(self, *args, **opts):
        from django.apps import apps

        User = apps.get_model("db", "User")
        Workspace = apps.get_model("db", "Workspace")
        WorkspaceMember = apps.get_model("db", "WorkspaceMember")
        Project = apps.get_model("db", "Project")
        ProjectMember = apps.get_model("db", "ProjectMember")

        slug = opts["workspace_slug"]
        ws = Workspace.objects.filter(slug=slug).first()
        if ws is None:
            raise CommandError(f"workspace slug={slug!r} not found")

        projects = list(
            Project.objects.filter(
                workspace=ws, identifier__in=opts["project_identifier"]
            )
        )
        missing = set(opts["project_identifier"]) - {p.identifier for p in projects}
        if missing:
            raise CommandError(
                f"projects not in workspace {slug!r}: {sorted(missing)}"
            )

        with transaction.atomic():
            user, created = User.objects.get_or_create(
                email=opts["email"],
                defaults={
                    "username": f"ai-agent-{uuid.uuid4().hex[:8]}",
                    "first_name": opts["display_name"],
                    "is_password_autoset": True,
                    "is_bot": True,
                },
            )

            agent, _ = AIAgent.objects.update_or_create(
                user=user,
                defaults={
                    "workspace": ws,
                    "enabled": not opts["disabled"],
                },
            )

            # Workspace membership — MEMBER, NOT ADMIN. Idempotent.
            WorkspaceMember.objects.update_or_create(
                workspace=ws,
                member=user,
                defaults={"role": ROLE.MEMBER.value, "is_active": True},
            )

            # Project memberships — MEMBER, only the listed projects.
            # Any pre-existing memberships in projects NOT in the list
            # are deactivated to keep the agent scope-tight.
            wanted_project_ids = {p.id for p in projects}
            ProjectMember.objects.filter(
                workspace=ws, member=user
            ).exclude(project_id__in=wanted_project_ids).update(is_active=False)

            for project in projects:
                ProjectMember.objects.update_or_create(
                    workspace=ws,
                    project=project,
                    member=user,
                    defaults={"role": ROLE.MEMBER.value, "is_active": True},
                )

        action = "created" if created else "updated"
        self.stdout.write(
            self.style.SUCCESS(
                f"agent {action}: user={user.id} workspace={ws.slug} "
                f"projects={[p.identifier for p in projects]} "
                f"enabled={agent.enabled}"
            )
        )
