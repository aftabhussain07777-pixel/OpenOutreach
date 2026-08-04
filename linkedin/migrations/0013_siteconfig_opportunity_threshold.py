from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('linkedin', '0012_siteconfig_conversation_ai_model'),
    ]

    operations = [
        migrations.AddField(
            model_name='siteconfig',
            name='opportunity_score_threshold',
            field=models.FloatField(default=0.8, help_text='Minimum opportunity score to trigger a notification (0.0 - 1.0).'),
        ),
    ]
