from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('activities', '0020_activity_end_time_activity_start_time'),
    ]

    operations = [
        migrations.AddField(
            model_name='activity',
            name='archived_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='归档时间'),
        ),
    ]
