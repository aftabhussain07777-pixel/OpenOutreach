"""Add ``Lead.industry`` field."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("crm", "0011_deal_next_check_pending_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="lead",
            name="industry",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
    ]
