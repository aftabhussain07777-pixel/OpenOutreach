# Generated migration to remove freemium fields

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('linkedin', '0007_siteconfig_llm_provider'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='campaign',
            name='is_freemium',
        ),
        migrations.RemoveField(
            model_name='campaign',
            name='action_fraction',
        ),
    ]
