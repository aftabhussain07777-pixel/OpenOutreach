from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('crm', '0017_deal_opportunity_assessment'),
    ]

    operations = [
        migrations.AddField(
            model_name='deal',
            name='opportunity_flagged',
            field=models.BooleanField(default=False, help_text='Set when the opportunity detector triggered a notification — human takeover in progress, agent auto-replies are paused'),
        ),
    ]
