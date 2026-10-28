"""备忘功能下线（2026-10-28，用户决定）：notes app 移除后的数据清理

- Tag scope='note' 的标签整体删除（宿主表同步 drop，标签成孤儿）；
- FTS 表里 module='note' 的索引行删除；
- notes_note / notes_note_tags 两张表 DROP——notes app 已从 INSTALLED_APPS
  移除，Django 不再管理这两张表，只能用 schema_editor 显式删。
  表存在性守卫：全新库上这两张表从未建过（历史迁移不再依赖 notes）。

回滚说明：本迁移不可逆。回退需先恢复 notes app 与其迁移历史，
再从 db.sqlite3 快照还原数据。
"""
from django.db import migrations


def cleanup(apps, schema_editor):
    Tag = apps.get_model('core', 'Tag')
    Tag.objects.filter(scope='note').delete()

    with schema_editor.connection.cursor() as cur:
        tables = {r[0] for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if 'core_fts' in tables:
            cur.execute("DELETE FROM core_fts WHERE module = 'note'")
        if 'notes_note_tags' in tables:
            cur.execute('DROP TABLE notes_note_tags')
        if 'notes_note' in tables:
            cur.execute('DROP TABLE notes_note')


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0016_alter_tag_color'),
    ]

    operations = [
        migrations.RunPython(cleanup, migrations.RunPython.noop),
    ]
