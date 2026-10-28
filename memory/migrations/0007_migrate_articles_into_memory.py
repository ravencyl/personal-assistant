"""把知识库文章整表迁入 Memory（type='document'）——两库合并的数据搬迁

口径（2026-10-28 用户决定知识库与记忆数据层合并）：
- Article → Memory：type='document'、status='active'、slug/tags/时间戳全保留
  （slug 不变 = 老 URL 零失效）；importance/confidence 等记忆字段用模型默认
- 时间戳回填必须走 queryset.update（created_at/updated_at 是 auto_now/auto_now_add，
  create 时传值会被重置回当前时间）
- 幂等：slug 已存在的跳过（重跑安全）
- ArticleVersion → MemoryVersion 同步搬迁
"""
from django.db import migrations


def migrate_articles(apps, schema_editor):
    Article = apps.get_model('knowledge', 'Article')
    ArticleVersion = apps.get_model('knowledge', 'ArticleVersion')
    Memory = apps.get_model('memory', 'Memory')
    MemoryVersion = apps.get_model('memory', 'MemoryVersion')

    existing_slugs = set(Memory.objects.exclude(slug__isnull=True)
                         .values_list('slug', flat=True))

    id_map = {}
    for article in Article.objects.all():
        if article.slug in existing_slugs:
            continue
        mem = Memory.objects.create(
            user_id=article.user_id,
            type='document',
            status='active',
            category='other',
            title=article.title,
            slug=article.slug,
            content=article.content,
            qmind_source_id=article.qmind_source_id,
            qmind_sync_hash=article.qmind_sync_hash,
        )
        mem.tags.set(article.tags.all())
        Memory.objects.filter(pk=mem.pk).update(
            created_at=article.created_at, updated_at=article.updated_at)
        existing_slugs.add(article.slug)
        id_map[article.pk] = mem.pk

    for version in ArticleVersion.objects.select_related('article'):
        mem_id = id_map.get(version.article_id)
        if mem_id is None:
            continue
        mv = MemoryVersion.objects.create(
            memory_id=mem_id,
            content=version.content,
            title=version.title,
            edit_note=version.edit_note,
        )
        MemoryVersion.objects.filter(pk=mv.pk).update(edited_at=version.edited_at)


def unmigrate_articles(apps, schema_editor):
    # 反向迁移不还原 Article（删表迁移在前向链更深处，此处只清掉迁入的文档条）
    Memory = apps.get_model('memory', 'Memory')
    Memory.objects.filter(type='document').delete()


class Migration(migrations.Migration):

    dependencies = [
        ('memory', '0006_memory_qmind_source_id_memory_qmind_sync_hash_and_more'),
        ('knowledge', '0006_articleversion'),
    ]

    operations = [
        migrations.RunPython(migrate_articles, unmigrate_articles),
    ]
