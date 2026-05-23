# TZ 5.2 — AIAgentActionLog audit table.

from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ("db", "0121_alter_estimate_type"),
        ("ai", "0003_aiagent"),
    ]

    operations = [
        migrations.CreateModel(
            name="AIAgentActionLog",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("issue_id", models.UUIDField()),
                ("tool_name", models.CharField(max_length=40)),
                ("input", models.JSONField(default=dict)),
                ("output", models.JSONField(blank=True, default=dict)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("applied", "applied"),
                            ("rejected", "rejected"),
                            ("error", "error"),
                        ],
                        max_length=20,
                    ),
                ),
                ("error", models.TextField(blank=True, default="")),
                (
                    "created_at",
                    models.DateTimeField(auto_now_add=True, db_index=True),
                ),
                (
                    "agent",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="action_logs",
                        to="ai.aiagent",
                    ),
                ),
                (
                    "project",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="ai_agent_action_logs",
                        to="db.project",
                    ),
                ),
                (
                    "workspace",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="ai_agent_action_logs",
                        to="db.workspace",
                    ),
                ),
            ],
            options={
                "verbose_name": "AI agent action",
                "verbose_name_plural": "AI agent actions",
                "db_table": "ai_agent_action_log",
                "indexes": [
                    models.Index(
                        fields=["issue_id", "created_at"],
                        name="ai_agent_log_issue_idx",
                    ),
                    models.Index(
                        fields=["workspace", "created_at"],
                        name="ai_agent_log_ws_idx",
                    ),
                ],
            },
        ),
    ]
