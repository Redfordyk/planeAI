# Angela — autonomous coding agent (sandbox-scoped). TZ Angela.1.
#
# Hand-written (the prod image bind-mounts ai/ read-only, so
# `manage.py makemigrations` can't write here). Two tables, both in our
# own `ai` schema; no columns added to Plane models (CLAUDE.md
# invariant 6). Isolation columns (workspace) carried on both tables.

import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("ai", "0007_issue_summary"),
        ("db", "0121_alter_estimate_type"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="AngelaRun",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("issue_id", models.UUIDField(blank=True, null=True)),
                ("target_repo", models.CharField(default="demo", max_length=120)),
                ("prompt", models.TextField(blank=True, default="")),
                (
                    "deploy_mode",
                    models.CharField(
                        choices=[
                            ("staging_gate", "staging_gate"),
                            ("autonomous_prod", "autonomous_prod"),
                            ("manual", "manual"),
                        ],
                        default="staging_gate",
                        max_length=20,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("queued", "queued"),
                            ("coding", "coding"),
                            ("reviewing", "reviewing"),
                            ("testing", "testing"),
                            ("deploying", "deploying"),
                            ("awaiting_approval", "awaiting_approval"),
                            ("succeeded", "succeeded"),
                            ("failed", "failed"),
                            ("cancelled", "cancelled"),
                        ],
                        default="queued",
                        max_length=24,
                    ),
                ),
                ("branch", models.CharField(blank=True, default="", max_length=160)),
                ("diff", models.TextField(blank=True, default="")),
                (
                    "review_verdict",
                    models.CharField(
                        choices=[
                            ("pending", "pending"),
                            ("approved", "approved"),
                            ("changes_requested", "changes_requested"),
                        ],
                        default="pending",
                        max_length=20,
                    ),
                ),
                ("test_passed", models.BooleanField(blank=True, null=True)),
                ("test_summary", models.TextField(blank=True, default="")),
                ("iterations", models.IntegerField(default=0)),
                ("deploy_target", models.CharField(blank=True, default="", max_length=20)),
                ("deploy_url", models.CharField(blank=True, default="", max_length=300)),
                ("wiki_url", models.CharField(blank=True, default="", max_length=300)),
                ("error", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "workspace",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="ai_angela_runs",
                        to="db.workspace",
                    ),
                ),
                (
                    "project",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="ai_angela_runs",
                        to="db.project",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="ai_angela_runs_created",
                        to="db.user",
                    ),
                ),
                (
                    "approved_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="ai_angela_runs_approved",
                        to="db.user",
                    ),
                ),
            ],
            options={
                "db_table": "ai_angela_run",
                "verbose_name": "Angela run",
                "verbose_name_plural": "Angela runs",
            },
        ),
        migrations.CreateModel(
            name="AngelaStep",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("workspace_id", models.UUIDField(db_index=True)),
                (
                    "phase",
                    models.CharField(
                        choices=[
                            ("plan", "plan"),
                            ("code", "code"),
                            ("review", "review"),
                            ("test", "test"),
                            ("deploy", "deploy"),
                            ("docs", "docs"),
                        ],
                        max_length=12,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("started", "started"),
                            ("ok", "ok"),
                            ("failed", "failed"),
                            ("skipped", "skipped"),
                        ],
                        max_length=12,
                    ),
                ),
                ("title", models.CharField(blank=True, default="", max_length=200)),
                ("detail", models.TextField(blank=True, default="")),
                ("iteration", models.IntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                (
                    "run",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="steps",
                        to="ai.angelarun",
                    ),
                ),
            ],
            options={
                "db_table": "ai_angela_step",
                "verbose_name": "Angela step",
                "verbose_name_plural": "Angela steps",
            },
        ),
        migrations.AddIndex(
            model_name="angelarun",
            index=models.Index(fields=["workspace", "created_at"], name="ai_angela_ws_time_idx"),
        ),
        migrations.AddIndex(
            model_name="angelarun",
            index=models.Index(fields=["status"], name="ai_angela_status_idx"),
        ),
        migrations.AddIndex(
            model_name="angelarun",
            index=models.Index(fields=["issue_id"], name="ai_angela_issue_idx"),
        ),
        migrations.AddIndex(
            model_name="angelastep",
            index=models.Index(fields=["run", "created_at"], name="ai_angela_step_run_idx"),
        ),
    ]
