from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('crm', '0015_add_lead_first_last_name'),
    ]

    operations = [
        migrations.AddField(
            model_name='deal',
            name='agent_conversation_summary',
            field=models.TextField(blank=True, default='', help_text='Last agent conversation summary'),
        ),
        migrations.AlterField(
            model_name='deal',
            name='agent_user_states',
            field=models.JSONField(blank=True, default=None, help_text='Last agent user states (RecipientState, ConversationState, RelationshipState, BeliefState)', null=True),
        ),
        migrations.RenameField(
            model_name='deal',
            old_name='agent_reasoning_summary',
            new_name='agent_action_reason',
        ),
    ]
