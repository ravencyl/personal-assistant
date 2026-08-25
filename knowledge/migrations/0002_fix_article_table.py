# 手写迁移：修复 knowledge_article 表缺失事故（幂等）
#
# 背景：0001_initial 曾被重写但文件名未变，django_migrations 中已标记 applied，
# 导致部分环境（本地/生产）从未真正创建 knowledge_article 表，而旧模型遗留的
# knowledge_knowledgearticle 表仍存在且有数据。
#
# 本迁移做三件事（均已做幂等处理，可安全在任意环境重复执行）：
# 1. 若 knowledge_article 表不存在，用 schema editor 按当前模型建表；
# 2. 用原生 SQL 幂等创建 knowledge_article_tags m2m 表；
# 3. 若旧表 knowledge_knowledgearticle 存在，将数据迁入新表
#    （先 PRAGMA table_info 确认字段名，映射 title→title、body→content，
#     时间字段沿用，slug 用 slugify 生成并处理冲突加 -2/-3 后缀）。

from django.db import migrations
from django.utils.text import slugify

OLD_TABLE = 'knowledge_knowledgearticle'
NEW_TABLE = 'knowledge_article'
M2M_TABLE = 'knowledge_article_tags'


def fix_article_table(apps, schema_editor):
    connection = schema_editor.connection
    existing_tables = set(connection.introspection.table_names())

    # 1. 建新表（已存在则跳过 —— 全新部署时 0001 已建好）
    Article = apps.get_model('knowledge', 'Article')
    if NEW_TABLE not in existing_tables:
        with connection.schema_editor() as se:
            se.create_model(Article)

    # 2. m2m 中间表（CREATE TABLE IF NOT EXISTS，幂等）
    with connection.cursor() as cursor:
        cursor.execute(
            f'''CREATE TABLE IF NOT EXISTS {M2M_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                article_id BIGINT NOT NULL REFERENCES {NEW_TABLE} ("id")
                    DEFERRABLE INITIALLY DEFERRED,
                tag_id INTEGER NOT NULL REFERENCES "taggit_tag" ("id")
                    DEFERRABLE INITIALLY DEFERRED,
                UNIQUE (article_id, tag_id)
            )'''
        )
        cursor.execute(
            f'CREATE INDEX IF NOT EXISTS {M2M_TABLE}_article_idx ON {M2M_TABLE} (article_id)'
        )
        cursor.execute(
            f'CREATE INDEX IF NOT EXISTS {M2M_TABLE}_tag_idx ON {M2M_TABLE} (tag_id)'
        )

    # 3. 旧表数据迁移（旧表不存在则跳过 —— 全新部署没有旧表）
    if OLD_TABLE not in existing_tables:
        return

    # 新表已有数据：视为已迁移过，幂等跳过
    if Article.objects.using(connection.alias).exists():
        return

    # 先 PRAGMA table_info 确认旧表实际字段名，再决定读取哪些列
    with connection.cursor() as cursor:
        cursor.execute(f'PRAGMA table_info({OLD_TABLE})')
        old_cols = {row[1] for row in cursor.fetchall()}
    if 'title' not in old_cols or 'body' not in old_cols:
        # 旧表结构与预期不符，放弃迁移避免误操作
        return

    select_cols = ['id', 'title', 'body']
    for col in ('user_id', 'created_at', 'updated_at'):
        if col in old_cols:
            select_cols.append(col)

    with connection.cursor() as cursor:
        cursor.execute(
            f'SELECT {", ".join(select_cols)} FROM {OLD_TABLE}'
        )
        rows = [dict(zip(select_cols, row)) for row in cursor.fetchall()]

    # 归属用户：旧表若无 user_id 列，优先挂到超级用户，其次最早的用户
    User = apps.get_model('auth', 'User')
    fallback_owner = User.objects.order_by('-is_superuser', 'id').first()
    if fallback_owner is None:
        return

    for row in rows:
        row_id = row['id']
        title = row['title'] or f'迁移文章 {row_id}'
        # 生成唯一 slug（重复加 -2/-3 数字后缀）
        base_slug = slugify(title, allow_unicode=True) or f'article-{row_id}'
        slug = base_slug
        counter = 2
        while Article.objects.using(connection.alias).filter(slug=slug).exists():
            slug = f'{base_slug}-{counter}'
            counter += 1

        user_id = row.get('user_id') or fallback_owner.id
        article = Article.objects.using(connection.alias).create(
            title=title,
            slug=slug,
            content=row['body'] or '',
            user_id=user_id,
        )
        # auto_now_add / auto_now 会覆盖传入值，用原始时间戳回填
        created_at = row.get('created_at')
        updated_at = row.get('updated_at')
        if created_at or updated_at:
            with connection.cursor() as cursor:
                cursor.execute(
                    f'UPDATE {NEW_TABLE} SET created_at = %s, updated_at = %s WHERE id = %s',
                    [created_at, updated_at, article.pk],
                )


def noop(apps, schema_editor):
    # 回滚无需处理（新表由 0001 负责）
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('knowledge', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(fix_article_table, noop),
    ]
