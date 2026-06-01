# Angela refine support: parent_run_id + title on AngelaRun.
#
# Hand-written (prod image bind-mounts ai/ read-only). Adds two nullable
# columns; no data migration needed.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("ai", "0008_angela"),
    ]

    operations = [
        migrations.AddField(
            model_name="angelarun",
            name="parent_run_id",
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="angelarun",
            name="title",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
    ]
