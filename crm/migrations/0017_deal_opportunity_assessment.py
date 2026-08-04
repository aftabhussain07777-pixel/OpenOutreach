from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('crm', '0016_add_conversation_summary_and_action_reason'),
    ]

    operations = [
        migrations.AddField(
            model_name='deal',
            name='opportunity_assessment',
            field=models.JSONField(blank=True, default=None, help_text='Last opportunity detector assessment (OpportunityAssessment)', null=True),
        ),
    ]
