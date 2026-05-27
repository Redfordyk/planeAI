# IssueSummary — per-issue AI summary cache (TZ 3.2 / P0.3 backlog).
#
# Hand-written (the prod image bind-mounts ai/ read-only, so
# `manage.py makemigrations` can't write here). One unique row per
# Plane Issue; cleanup of orphan rows happens via ai/signals.py when
# the upstream Issue is hard-deleted.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("ai", "0006_orchestrator"),
    ]

    operations = [
        migrations.CreateModel(
            name="IssueSummary",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("issue_id", models.UUIDField(unique=True, db_index=True)),
                ("workspace_id", models.UUIDField(db_index=True)),
                ("content_hash", models.CharField(max_length=64)),
                ("summary_text", models.TextField()),
                ("model_used", models.CharField(max_length=60)),
                ("input_tokens", models.IntegerField(default=0)),
                ("output_tokens", models.IntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "db_table": "ai_issue_summary",
                "verbose_name": "AI issue summary",
                "verbose_name_plural": "AI issue summaries",
            },
        ),
        migrations.AddIndex(
            model_name="issuesummary",
            index=models.Index(
                fields=["workspace_id", "updated_at"],
                name="ai_issue_su_workspa_idx",
            ),
        ),
    ]
