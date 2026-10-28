"""FTS5 全文搜索索引

SQLite FTS5 虚拟表同步与搜索。对 Activity / Article 两个模块做全文索引，
搜索时优先走 FTS5（BM25 排序），不可用时静默降级为 icontains。

信号同步：post_save / post_delete 自动更新 FTS 行。
"""
import logging

from django.db import connection

logger = logging.getLogger(__name__)

FTS_TABLE = 'core_fts'

# ── 索引同步 ──────────────────────────────────────────────────────────────


def _tags_str(obj):
    """把对象的 tags 拼成空格分隔字符串（FTS 索引用）"""
    try:
        return ' '.join(t.name for t in obj.tags.all())
    except Exception:
        return ''


def sync_to_fts(sender, instance, **kwargs):
    """post_save 信号：同步一行到 FTS 表"""
    module = _module_for_instance(sender, instance)
    if not module:
        return
    try:
        tags = _tags_str(instance)
        title = getattr(instance, 'title', '') or getattr(instance, 'name', '') or ''
        content = getattr(instance, 'content', '') or getattr(instance, 'description', '') or ''
        user_id = instance.user_id

        with connection.cursor() as cursor:
            # 先删旧行（update 场景），再插新行
            cursor.execute(
                f'DELETE FROM {FTS_TABLE} WHERE module=%s AND object_id=%s',
                [module, instance.pk]
            )
            cursor.execute(
                f'INSERT INTO {FTS_TABLE}(module, object_id, user_id, title, content, tags)'
                f' VALUES (%s, %s, %s, %s, %s, %s)',
                [module, instance.pk, user_id, title, content, tags]
            )
    except Exception:
        logger.warning('FTS 同步失败 module=%s id=%s', module, instance.pk, exc_info=True)


def remove_from_fts(sender, instance, **kwargs):
    """post_delete 信号：从 FTS 表删除一行"""
    module = _module_for_instance(sender, instance)
    if not module:
        return
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f'DELETE FROM {FTS_TABLE} WHERE module=%s AND object_id=%s',
                [module, instance.pk]
            )
    except Exception:
        logger.warning('FTS 删除失败 module=%s id=%s', module, instance.pk, exc_info=True)


def _module_for_instance(sender, instance):
    """根据信号 sender（+实例类型）返回模块名；不属于任何索引模块返回 None。

    Memory/Document 双 sender（proxy 分离）：Memory 存储两库合并后的统一表，
    只有 type='document' 的行按 'article' 模块入 FTS，fact 条不入。
    """
    name = f'{sender._meta.app_label}.{sender._meta.model_name}'
    if name == 'knowledge.document':
        return 'article'
    if name == 'memory.memory':
        return 'article' if getattr(instance, 'type', '') == 'document' else None
    return {
        'activities.activity': 'activity',
    }.get(name)


# ── 初始填充（迁移后调用） ────────────────────────────────────────────────

def populate_fts():
    """全量重建 FTS 索引（迁移后调用一次）"""
    from activities.models import Activity
    from knowledge.models import Document

    count = 0
    with connection.cursor() as cursor:
        cursor.execute(f'DELETE FROM {FTS_TABLE}')

    for Model, module in [(Activity, 'activity'), (Document, 'article')]:
        try:
            rows = []
            for obj in Model.objects.all().iterator():
                title = getattr(obj, 'title', '') or getattr(obj, 'name', '') or ''
                content = getattr(obj, 'content', '') or getattr(obj, 'description', '') or ''
                tags = _tags_str(obj)
                rows.append((module, obj.pk, obj.user_id, title, content, tags))
                count += 1
        except Exception:
            # 目标表还不存在（知识库并入记忆后 Document 实体在 memory app，
            # 全新库上 core.0012 可能先于 memory 建表执行）。空库本就无数据可填，
            # 后续写入由 post_save 信号同步，跳过即可；真有数据缺失时手动跑一次
            # populate_fts 即补齐
            logger.warning('FTS 填充跳过 %s（表可能尚未建立）', module, exc_info=True)
            continue
        if rows:
            with connection.cursor() as cursor:
                cursor.executemany(
                    f'INSERT INTO {FTS_TABLE}(module, object_id, user_id, title, content, tags)'
                    f' VALUES (%s, %s, %s, %s, %s, %s)',
                    rows
                )
    logger.info('FTS 索引填充完成，共 %d 条', count)


# ── 搜索 ──────────────────────────────────────────────────────────────────

def fts_search(user, query, limit_per_module=5):
    """FTS5 全文搜索，返回按模块分组的 object_id 列表。

    返回格式: {'activity': [id, ...], 'article': [id, ...]}
    FTS 不可用时返回 None（调用方降级为 icontains）。
    """
    if not query or not query.strip():
        return None

    # 检查 FTS 表是否存在
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT name FROM sqlite_master WHERE type='table' AND name=%s",
                [FTS_TABLE]
            )
            if not cursor.fetchone():
                return None
    except Exception:
        return None

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT module, object_id, rank FROM {FTS_TABLE}"
                f" WHERE {FTS_TABLE} MATCH %s AND user_id = %s"
                f" ORDER BY rank LIMIT %s",
                [query.strip(), user.pk, limit_per_module * 3 * 3]
                # 取多一些，后面按模块分组截取
            )
            rows = cursor.fetchall()
    except Exception:
        logger.warning('FTS 搜索失败，降级为 icontains', exc_info=True)
        return None

    results = {'activity': [], 'article': []}
    for module, obj_id, rank in rows:
        if module in results and len(results[module]) < limit_per_module:
            results[module].append(obj_id)

    return results
