# TZ 5.1 — AIAgent table.

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ("db", "0121_alter_estimate_type"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("ai", "0002_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="AIAgent",
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
                ("enabled", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "user",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="ai_agent",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "workspace",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="ai_agents",
                        to="db.workspace",
                    ),
                ),
            ],
            options={
                "verbose_name": "AI agent",
                "verbose_name_plural": "AI agents",
                "db_table": "ai_agent",
                "indexes": [
                    models.Index(
                        fields=["workspace", "enabled"],
                        name="ai_agent_ws_enabled_idx",
                    ),
                ],
            },
        ),
    ]
