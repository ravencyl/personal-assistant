"""站内通知的唯一写入口与读取助手（2026-11-12）。

后台任务（如活动描述 AI 优化）完成后调 notify() 落一条通知；PC 顶栏铃铛与移动
「我的」徽标读 unread_count / recent 渲染。所有查询都是**个人口径**
（filter(user=...)，超管也不混他人通知），与侧栏对话同款「这堆数据是谁的」判据，
不走 visible_qs（那是管理视角的列表可见范围）。

容错铁律：notify 失败只 warning，绝不把调用它的主流程（优化写库、状态变更等）弄挂。
"""
import logging

from .models import Notification

logger = logging.getLogger(__name__)


def notify(user, title, *, kind='system', body='', link=''):
    """落一条站内通知（唯一写入口）。失败仅告警，返回 None，不抛异常。

    title 面向用户（口语结论），link 必须是服务端 reverse() 产出的可读路径，
    前端只跳转不拼接。kind 见 Notification.KIND_CHOICES。
    """
    if user is None or not getattr(user, 'pk', None):
        return None
    try:
        return Notification.objects.create(
            user=user,
            kind=kind,
            title=str(title)[:200],
            body=str(body or ''),
            link=str(link or '')[:500],
        )
    except Exception as e:  # 通知是附加信息，落库失败不能阻断主流程
        logger.warning(f'站内通知写入失败 user={getattr(user, "pk", None)}: {e}')
        return None


def unread_count(user):
    """当前用户未读通知数（个人口径）。"""
    if user is None or not getattr(user, 'pk', None):
        return 0
    return Notification.objects.filter(user=user, is_read=False).count()


def recent(user, limit=30):
    """当前用户最近的通知（含已读，按时间倒序，个人口径）。"""
    if user is None or not getattr(user, 'pk', None):
        return Notification.objects.none()
    return Notification.objects.filter(user=user).order_by('-created_at')[:limit]