"""Article → QMind 的信号同步层

Article 是本地唯一事实源，QMind 是只读镜像：保存后异步上传、删除后异步清理。
同步走后台线程（项目没有常驻 Celery worker，线程 + 短超时是最小依赖方案）；
任何失败只记 warning，绝不影响文章保存本身（容错铁律）。

单元测试进程（manage.py test）直接跳过外呼，避免 500+ 测试里混入真实网络调用。
"""
import logging
import sys
import threading

from django.conf import settings

logger = logging.getLogger(__name__)


def _enabled():
    if 'test' in sys.argv:
        return False
    from . import qmind
    return qmind.configured()


def article_saved(sender, instance, **kwargs):
    """post_save：内容指纹变化才同步（纯浏览/字段无关保存不发请求）"""
    if not _enabled():
        return
    if instance.qmind_sync_hash == instance.sync_hash() and instance.qmind_source_id:
        return
    threading.Thread(target=_sync_article, args=(instance.pk,), daemon=True).start()


def article_deleted(sender, instance, **kwargs):
    """post_delete：清理云端源（没有 source_id 说明从未同步过，直接跳过）"""
    if not _enabled() or not instance.qmind_source_id:
        return
    threading.Thread(target=_delete_source, args=(instance.qmind_source_id, instance.pk), daemon=True).start()


def _sync_article(pk):
    from . import qmind
    from .models import Article

    try:
        article = Article.objects.get(pk=pk)
        source_id = qmind.sync_article(article.title, article.content,
                                       old_source_id=article.qmind_source_id)
        # update 而非 save：避免再次触发 post_save 造成同步循环
        Article.objects.filter(pk=pk).update(
            qmind_source_id=source_id, qmind_sync_hash=article.sync_hash())
        logger.info(f'QMind 同步成功：《{article.title}》 source={source_id}')
    except Article.DoesNotExist:
        pass  # 保存后立刻被删，post_delete 会自己清理
    except Exception as e:
        logger.warning(f'QMind 同步失败 (article {pk}): {e}')


def _delete_source(source_id, pk):
    from . import qmind

    try:
        qmind.delete_source(source_id)
        logger.info(f'QMind 源已删除：article={pk} source={source_id}')
    except Exception as e:
        logger.warning(f'QMind 删源失败 (article {pk}): {e}')
