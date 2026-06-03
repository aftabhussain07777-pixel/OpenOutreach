"""Drop ``LinkedInProfile.connect_weekly_limit``.

The planner pre-commits to a daily budget; LinkedIn's own weekly ceiling
surfaces at the handler boundary via ``ReachedConnectionLimit``.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("linkedin", "0008_remove_freemium_fields"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="linkedinprofile",
            name="connect_weekly_limit",
        ),
    ]
