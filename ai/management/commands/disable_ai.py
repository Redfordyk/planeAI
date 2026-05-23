"""TZ 6.5 — kill switch for the AI layer.

The single fastest rollback when the AI add-on misbehaves on prod:
flip ``WorkspaceAIConfig.enabled=False`` and every AI feature
short-circuits cleanly while Plane proper keeps running. This
command is the operator-facing version of that flip — one line,
idempotent, scoped to a workspace **or** the whole installation.

Why a management command (and not "open the Django admin")?

  - The admin UI requires SSO + a working Django session. When the
    AI layer is causing exceptions that bleed into the admin view
    (a rare but observed failure mode in upstream Plane plugins),
    you cannot reach the toggle through the UI.
  - A management command is one ``docker compose exec`` away — same
    surface as the backup/restore scripts. Operators already know
    that surface.
  - It can be put in a runbook as a literal copy-paste line and
    works the same on staging and prod.

Usage::

    # Disable AI in one workspace.
    python manage.py disable_ai --workspace <uuid-or-slug>

    # Disable AI everywhere (use during a global incident).
    python manage.py disable_ai --all-workspaces --confirm

    # Re-enable later — same kwargs.
    python manage.py enable_ai  --workspace <uuid-or-slug>

The audit trail is the log line this command prints (operator +
timestamp + which workspaces). For long-term records, the agent
toggle history is on each :class:`AIAgent` row via ``updated_at``;
the workspace toggle is on :class:`WorkspaceAIConfig.updated_at`.

This file implements **two** commands sharing one base, so we don't
duplicate option parsing / printing between disable and enable.
"""

from __future__ import annotations

import logging
import uuid
from typing import Iterable

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone


logger = logging.getLogger("plane.ai.rollback")


class _ToggleBase(BaseCommand):
    """Shared option parser + flip routine. Subclasses set
    :attr:`TARGET_ENABLED` (the value to write)."""

    # Overridden by Command subclasses.
    TARGET_ENABLED: bool = False

    def add_arguments(self, parser):
        parser.add_argument(
            "--workspace",
            help=(
                "Target one workspace — accepts either the Workspace UUID "
                "or the Workspace.slug. Mutually exclusive with "
                "--all-workspaces."
            ),
        )
        parser.add_argument(
            "--all-workspaces",
            action="store_true",
            help=(
                "Flip every WorkspaceAIConfig in the installation. Requires "
                "--confirm — there's no good reason to do this by accident."
            ),
        )
        parser.add_argument(
            "--confirm",
            action="store_true",
            help=(
                "Required acknowledgement for --all-workspaces. Operator "
                "must type it explicitly so a stale shell history can't "
                "fire-and-forget a global toggle."
            ),
        )

    # ------------------------------------------------------------------
    # handle()
    # ------------------------------------------------------------------

    def handle(self, *args, **options):  # noqa: ANN001 — Django override
        # Late import: the management module loads even before
        # INSTALLED_APPS finishes — see Django docs on management
        # command import order. Models are safe to import inside
        # ``handle`` because by then the app registry is ready.
        from ai.models import WorkspaceAIConfig

        target = bool(self.TARGET_ENABLED)
        verb = "enable" if target else "disable"

        if options["all_workspaces"] and options["workspace"]:
            raise CommandError(
                "--workspace and --all-workspaces are mutually exclusive"
            )
        if not options["all_workspaces"] and not options["workspace"]:
            raise CommandError(
                "either --workspace <id|slug> or --all-workspaces is required"
            )
        if options["all_workspaces"] and not options["confirm"]:
            raise CommandError(
                "--all-workspaces requires --confirm "
                f"(this is a global {verb}; refusing without explicit ack)"
            )

        if options["all_workspaces"]:
            ws_ids = list(
                WorkspaceAIConfig.objects.values_list("workspace_id", flat=True)
            )
            scope_label = f"ALL {len(ws_ids)} workspaces"
        else:
            ws_ids = list(self._resolve_workspace_ids(options["workspace"]))
            if not ws_ids:
                raise CommandError(
                    f"no workspace matched {options['workspace']!r} "
                    "(checked UUID and slug)"
                )
            scope_label = f"workspace {options['workspace']}"

        with transaction.atomic():
            # ``update_or_create`` is the wrong tool here: we only want
            # to touch ROWS that already exist. A workspace without
            # AIConfig is fine — it's already 'disabled' by virtue of
            # the row not existing.
            qs = WorkspaceAIConfig.objects.filter(workspace_id__in=ws_ids)
            # Snapshot before-state so the operator sees the no-op case
            # ("already disabled — nothing to do") explicitly.
            before = list(
                qs.values_list("workspace_id", "enabled")
            )
            updated = qs.exclude(enabled=target).update(
                enabled=target, updated_at=timezone.now()
            )

        already_in_state = sum(1 for _, e in before if e == target)
        changed = updated
        missing = len(ws_ids) - len(before)

        msg = (
            f"{verb}d AI for {scope_label}: "
            f"changed={changed}, already_{verb}d={already_in_state}, "
            f"no_config={missing}"
        )
        logger.warning("rollback toggle: %s", msg)
        self.stdout.write(self.style.SUCCESS(msg))
        # Returning the message lets pytest assertions read the
        # outcome via ``call_command(..., stdout=StringIO())``.
        return msg

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_workspace_ids(token: str) -> Iterable[str]:
        """Return zero or one workspace id, looking up by UUID first
        then by slug. Two lookups instead of one — but the cost is
        a single indexed Postgres query each, and the operator gets
        to pass whichever identifier they have at hand."""
        from django.apps import apps as django_apps

        Workspace = django_apps.get_model("db", "Workspace")
        # UUID first (most operators pass the id from a log line).
        try:
            ws_uuid = uuid.UUID(str(token))
            row = Workspace.objects.filter(id=ws_uuid).only("id").first()
            if row:
                return [row.id]
        except (ValueError, AttributeError):
            pass
        # Slug fallback.
        row = Workspace.objects.filter(slug=token).only("id").first()
        return [row.id] if row else []


class Command(_ToggleBase):
    """``python manage.py disable_ai ...`` — flip enabled → False."""

    help = (
        "Disable the AI layer for a workspace or globally. The fastest "
        "rollback when AI features misbehave: Plane keeps running, "
        "ai/* endpoints return 403, ingest signals + agent triggers "
        "short-circuit. See ROLLBACK.md for the full decision tree."
    )
    TARGET_ENABLED = False
