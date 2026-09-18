"""FTS5 全文搜索虚拟表

创建 core_fts 虚拟表用于 Activity / Article / Note 的全文索引。
迁移后自动填充现有数据。
"""
from django.db import migrations


def create_fts(apps, schema_editor):
    """创建 FTS5 虚拟表并填充现有数据"""
    from django.db import connection
    with connection.cursor() as cursor:
        cursor.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS core_fts USING fts5(
                module, object_id, user_id UNINDEXED,
                title, content, tags,
                tokenize='unicode61'
            )
        """)
    # 填充现有数据
    from core.fts import populate_fts
    populate_fts()


def drop_fts(apps, schema_editor):
    """删除 FTS5 虚拟表"""
    from django.db import connection
    with connection.cursor() as cursor:
        cursor.execute("DROP TABLE IF EXISTS core_fts")


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0011_pushschedule'),
    ]

    operations = [
        migrations.RunPython(create_fts, drop_fts),
    ]
