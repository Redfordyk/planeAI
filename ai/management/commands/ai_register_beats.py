"""Register Celery Beat schedules for the orchestrator.

Plane uses `django_celery_beat.schedulers.DatabaseScheduler`, so
PeriodicTask rows in the DB define the schedule — code-level
`beat_schedule` is ignored. This command idempotently creates two
recurring tasks:

  ai-orchestrator-hourly-scan
    every hour at :15 → ai.orchestrator_tick_all_workspaces(scan_only=True)
    drives MONITOR + ESCALATOR for every workspace's active goals.

  ai-orchestrator-weekly-tick
    Fridays 17:00 UTC → ai.orchestrator_tick_all_workspaces(scan_only=False)
    drives COMMUNICATOR + ANALYST status reports.

Run with::

    python manage.py ai_register_beats

Idempotent — re-running updates the rows in place. To remove, set
``enabled=False`` from the admin or delete the row.
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Idempotently register periodic Celery Beat tasks for the orchestrator."

    def handle(self, *args, **opts):
        try:
            from django_celery_beat.models import CrontabSchedule, PeriodicTask
        except Exception as exc:
            self.stderr.write(f"django_celery_beat not installed: {exc}")
            return

        # 1) Hourly scan at :15 of every hour
        hourly, _ = CrontabSchedule.objects.get_or_create(
            minute="15", hour="*", day_of_week="*",
            day_of_month="*", month_of_year="*",
        )
        PeriodicTask.objects.update_or_create(
            name="ai-orchestrator-hourly-scan",
            defaults={
                "crontab": hourly,
                "task": "ai.orchestrator_tick_all_workspaces",
                "args": json.dumps([True]),  # scan_only=True
                "enabled": True,
                "description": "Orchestrator MONITOR sweep (every hour).",
            },
        )
        self.stdout.write(self.style.SUCCESS("✓ hourly scan registered"))

        # 2) Weekly tick — Fridays at 17:00 UTC (~20:00 MSK)
        weekly, _ = CrontabSchedule.objects.get_or_create(
            minute="0", hour="17", day_of_week="5",
            day_of_month="*", month_of_year="*",
        )
        PeriodicTask.objects.update_or_create(
            name="ai-orchestrator-weekly-tick",
            defaults={
                "crontab": weekly,
                "task": "ai.orchestrator_tick_all_workspaces",
                "args": json.dumps([False]),  # scan_only=False
                "enabled": True,
                "description": "Orchestrator weekly digest (Fri 17:00 UTC).",
            },
        )
        self.stdout.write(self.style.SUCCESS("✓ weekly tick registered"))

        # 3) Hourly index reconciliation — catches issues whose post_save
        #    signal didn't enqueue (workspace created before AI enabled,
        #    signal disconnected during migration, etc.).
        reconcile = hourly  # reuse the :15 hourly schedule
        PeriodicTask.objects.update_or_create(
            name="ai-reconcile-index",
            defaults={
                "crontab": reconcile,
                "task": "ai.reconcile_index",
                "args": json.dumps([]),
                "enabled": True,
                "description": "Backfill missing DocumentChunks for unindexed issues.",
            },
        )
        self.stdout.write(self.style.SUCCESS("✓ hourly index reconcile registered"))

        self.stdout.write("Beat will pick up changes within 30s.")
