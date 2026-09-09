# 知识库文章标签从 taggit 换到 core.Tag M2M（RemoveField+AddField，理由见
# activities.0015 注释；数据由 activities.0016 data migration 搬运）。
#
# ⚠️ 必须先 DROP knowledge_article_tags：0002_fix_article_table 曾用原生 SQL
# 幂等建过同名中间表（引用 taggit_tag）。RemoveField 对 taggit 的显式 through
# （taggit.TaggedItem）不会 drop 表，而新 M2M 的默认中间表恰好也叫这个名——
# 不先 DROP 则 AddField 直接报 'table already exists'（全新库/测试库必炸）。
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0008_initial'),
        ('knowledge', '0004_article_qmind_source_id_article_qmind_sync_hash'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='article',
            name='tags',
        ),
        migrations.RunSQL(
            'DROP TABLE IF EXISTS knowledge_article_tags',
            migrations.RunSQL.noop,
        ),
        migrations.AddField(
            model_name='article',
            name='tags',
            field=models.ManyToManyField(blank=True, to='core.tag', verbose_name='标签'),
        ),
    ]
