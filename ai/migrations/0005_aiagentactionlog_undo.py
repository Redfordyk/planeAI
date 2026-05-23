# TZ 5.6 — add undo bookkeeping fields to the agent action log.
#
# Two columns on ``ai_agent_action_log``:
#   - ``undone_at``: NULL = action is still in effect; timestamp = the
#     instant the TZ 5.6 undo endpoint rolled the action back.
#   - ``undone_by``: db.User who triggered the undo. SET_NULL so a
#     deleted user does not orphan the row — the timestamp alone is
#     enough for an "this got rolled back" audit signal.
#
# We intentionally do NOT delete or rewrite the original row: the
# audit log is append-only, and "this action existed and was undone"
# is a distinct answer from "this action never happened".

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("db", "0121_alter_estimate_type"),
        ("ai", "0004_aiagentactionlog"),
    ]

    operations = [
        migrations.AddField(
            model_name="aiagentactionlog",
            name="undone_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="aiagentactionlog",
            name="undone_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="ai_agent_actions_undone",
                to="db.user",
            ),
        ),
    ]
