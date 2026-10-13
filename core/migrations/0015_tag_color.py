from django.db import migrations, models

import core.tags


def backfill_activity_tag_colors(apps, schema_editor):
    """存量活动标签补底色：一律随机生成（走与运行时同一函数），admin 可再改"""
    Tag = apps.get_model('core', 'Tag')
    for tag in Tag.objects.filter(scope='activity').exclude(color__gt=''):
        tag.color = core.tags.random_tag_color()
        tag.save(update_fields=['color'])


def unbackfill(apps, schema_editor):
    Tag = apps.get_model('core', 'Tag').objects.all().update(color='')


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0014_alter_pushschedule_push_type'),
    ]

    operations = [
        migrations.AddField(
            model_name='tag',
            name='color',
            field=models.CharField(blank=True, default='', help_text='活动标签的展示底色，留空保存时自动随机生成', max_length=7, verbose_name='底色'),
        ),
        migrations.RunPython(backfill_activity_tag_colors, unbackfill),
    ]
