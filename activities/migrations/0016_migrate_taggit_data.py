"""把 taggit 数据搬入 core.Tag + 各模型 M2M（一次性，含脏数据清洗）

搬运规则：
- 孤儿标签（宿主对象已删）不搬——历史上 13 个标签里 8 个是孤儿，直接消失；
- 逗号/顿号粘连的脏标签拆分（「a，b」是旧解析事故一次灌入的两段）；
- 同名去重（M2M 天然）；跨用户的同名标签合并为一条 scope 内 Tag 实体，
  展示与筛选都按用户对象过滤，互不干扰；
- taggit_tag/ taggit_taggeditem 两张表在搬运后 DROP（taggit 已从
  INSTALLED_APPS 移除，不再有模型管理它们）；content_type 表里的
  taggit 记录一并清掉。

回滚说明：本迁移不可逆（与 Activity.cost→Expense 拆分同性质），
回退需从各模型 M2M 反向重建 taggit 结构。
"""
import re

from django.db import migrations

_SPLIT_RE = re.compile(r'[,，、]')

# taggit TaggedItem.content_type.model → scope（knowledge 的模型类是 Article）
_CT_SCOPE = {
    'activity': 'activity',
    'note': 'note',
    'knowledgearticle': 'knowledge',
}


def migrate_tags(apps, schema_editor):
    Tag = apps.get_model('core', 'Tag')
    ContentType = apps.get_model('contenttypes', 'ContentType')
    Activity = apps.get_model('activities', 'Activity')
    Note = apps.get_model('notes', 'Note')
    Article = apps.get_model('knowledge', 'Article')

    ct_scope = {ct.id: _CT_SCOPE[ct.model] for ct in ContentType.objects.all()
                if ct.model in _CT_SCOPE}
    host_models = {'activity': Activity, 'note': Note, 'knowledge': Article}

    # 读原始数据（此时尚未依赖 taggit app 在 INSTALLED_APPS 里）
    with schema_editor.connection.cursor() as cur:
        cur.execute(
            'SELECT ti.content_type_id, ti.object_id, t.name '
            'FROM taggit_taggeditem ti JOIN taggit_tag t ON t.id = ti.tag_id')
        rows = cur.fetchall()

    for ct_id, obj_id, name in rows:
        scope = ct_scope.get(ct_id)
        if scope is None:
            continue
        model = host_models[scope]
        try:
            host = model.objects.get(pk=int(obj_id))
        except (model.DoesNotExist, ValueError, TypeError):
            continue  # 孤儿标签：宿主已删，不搬
        # 清洗：粘连拆分 + 去空（去重由 M2M 保证）
        for part in _SPLIT_RE.split(str(name)):
            part = part.strip()
            if not part:
                continue
            tag, _created = Tag.objects.get_or_create(
                scope=scope, name=part[:50], defaults={'is_active': True})
            host.tags.add(tag)


def drop_taggit_tables(apps, schema_editor):
    """taggit 出局：两张数据表 + content_type/permission 记录一并清理

    顺序关键：必须先删引用 taggit content_type 的 auth_permission 行，
    再删 content_type——否则 SQLite 事务末的外键校验直接报 IntegrityError
    （实测踩过：'auth_permission.content_type_id contains a value 51'）。
    """
    with schema_editor.connection.cursor() as cur:
        cur.execute(
            "DELETE FROM auth_permission WHERE content_type_id IN "
            "(SELECT id FROM django_content_type WHERE app_label = 'taggit')")
        cur.execute("DELETE FROM django_content_type WHERE app_label = 'taggit'")
        cur.execute('DROP TABLE IF EXISTS taggit_taggeditem')
        cur.execute('DROP TABLE IF EXISTS taggit_tag')


class Migration(migrations.Migration):

    dependencies = [
        ('activities', '0015_expense_tags_alter_activity_tags'),
        ('notes', '0003_alter_note_tags'),
        ('knowledge', '0005_alter_article_tags'),
    ]

    operations = [
        migrations.RunPython(migrate_tags, migrations.RunPython.noop),
        migrations.RunPython(drop_taggit_tables, migrations.RunPython.noop),
    ]
