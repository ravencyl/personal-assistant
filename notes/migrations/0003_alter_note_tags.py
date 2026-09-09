# 备忘录标签从 taggit 换到 core.Tag M2M（RemoveField+AddField，理由见
# activities.0015 注释；数据由 activities.0016 data migration 搬运）。
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('notes', '0002_note_notes_note_user_id_d67ab6_idx'),
        ('core', '0008_initial'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='note',
            name='tags',
        ),
        migrations.AddField(
            model_name='note',
            name='tags',
            field=models.ManyToManyField(blank=True, to='core.tag', verbose_name='标签'),
        ),
    ]
