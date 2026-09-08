"""QMind 云端知识库客户端

方案 B 的接入层：Article 仍是本地唯一事实源，本模块只负责两件事——
① 语义检索（retrieve）：站内关键词检索的升级，供对话注入与 knowledge.search 使用；
② 源同步（sync/delete）：Article 保存/删除信号触发的异步镜像上传。

API 契约（2026-09-08 用转发代理逐请求实测验证）：
- POST /api/v1/jobToken/exchange   {"personal_token": "pt-..."} → {token(jt), expires_in(ms)}
- POST /sash/api/v1/notebooks/{nb}/retrieve          {"query","topK"} → {chunks[],...}
- POST /sash/api/v1/notebooks/{nb}/sources/upload    multipart(title/retain_original_file/file)
  → 200 {id,...}；409 AlreadyExists = 服务端按内容 SHA 去重（视为已同步成功）
- DELETE /sash/api/v1/notebooks/{nb}/sources/{id}    → {"success": true}
- GET /sash/api/v1/notebooks/{nb}/sources            → {sources: [...]}

容错铁律：本模块任何失败都向上抛 QMindError，由调用方降级（检索层降级回本地
关键词检索；同步层只记 warning），绝不阻断对话或文章保存。
"""
import logging
import threading

import httpx
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

TOKEN_CACHE_KEY = 'qmind_job_token'
# 检索挂在对话请求链路上，超时必须远小于 chat.AI_WAIT_TIMEOUT（90s）
RETRIEVE_TIMEOUT = 5.0
SYNC_TIMEOUT = 30.0

_token_lock = threading.Lock()


class QMindError(Exception):
    """QMind API 调用失败（网络/鉴权/非 200），调用方应降级而不是向上冒泡"""


def notebook_id():
    return getattr(settings, 'QMIND_NOTEBOOK_ID', '') or ''


def configured():
    """QMIND_NOTEBOOK_ID 与 QODER_ACCESS_TOKEN（即 pt- 个人令牌）都配置了才启用"""
    return bool(notebook_id() and getattr(settings, 'QODER_ACCESS_TOKEN', ''))


def _exchange():
    """pt- 个人令牌换短期 job token（24h），缓存留 10 分钟余量"""
    resp = httpx.post(
        f"{settings.QMIND_SASH_URL}/api/v1/jobToken/exchange",
        json={'personal_token': settings.QODER_ACCESS_TOKEN},
        timeout=SYNC_TIMEOUT,
    )
    if resp.status_code != 200:
        raise QMindError(f'exchange 返回 {resp.status_code}: {resp.text[:120]}')
    data = resp.json()
    token = data.get('token')
    if not token:
        raise QMindError('exchange 响应缺少 token 字段')
    ttl = max(int(data.get('expires_in', 86400000) / 1000) - 600, 3600)
    cache.set(TOKEN_CACHE_KEY, token, ttl)
    return token


def _token():
    """job token 缓存读取（双检锁防并发重复 exchange，冷启动一次约 10s 不可接受多次）"""
    token = cache.get(TOKEN_CACHE_KEY)
    if token:
        return token
    with _token_lock:
        token = cache.get(TOKEN_CACHE_KEY)
        if token:
            return token
        return _exchange()


def _request(method, path, *, json_body=None, data=None, files=None, timeout=SYNC_TIMEOUT, retry_auth=True):
    """带 401 自动重换 token 一次的请求封装"""
    try:
        resp = httpx.request(
            method, f"{settings.QMIND_SASH_URL}{path}",
            json=json_body, data=data, files=files,
            headers={'Authorization': f'Bearer {_token()}'},
            timeout=timeout,
        )
    except httpx.HTTPError as e:
        raise QMindError(f'请求 {path} 失败: {e}') from e
    if resp.status_code == 401 and retry_auth:
        # job token 被服务端提前吊销/过期：清缓存换新再试一次
        cache.delete(TOKEN_CACHE_KEY)
        return _request(method, path, json_body=json_body, data=data, files=files,
                        timeout=timeout, retry_auth=False)
    return resp


def _chunk_title(chunk):
    """云端 chunk 用 sourceTitle（带 .md 尾巴）标源，去掉后缀得到本地文章标题"""
    title = (chunk.get('sourceTitle') or chunk.get('title') or '').strip()
    if title.endswith('.md'):
        title = title[:-3]
    return title


def retrieve(query, top_k=3):
    """语义检索知识库，返回 [{'title','snippet','score','uri'}]

    未配置（configured() 为 False）返回 None，调用方据此走本地降级；
    配置了但调用失败抛 QMindError，同样由调用方降级。
    """
    if not configured():
        return None
    resp = _request('POST', f'/sash/api/v1/notebooks/{notebook_id()}/retrieve',
                    json_body={'query': query, 'topK': top_k},
                    timeout=RETRIEVE_TIMEOUT)
    if resp.status_code != 200:
        raise QMindError(f'retrieve 返回 {resp.status_code}: {resp.text[:120]}')
    chunks = resp.json().get('chunks') or []
    return [
        {
            'title': _chunk_title(c),
            'snippet': (c.get('content') or c.get('snippet') or '').strip(),
            'score': c.get('score'),
            'uri': c.get('sourceUri') or c.get('uri') or '',
        }
        for c in chunks
        if (c.get('content') or c.get('snippet'))
    ]


def _find_source_id_by_title(title):
    resp = _request('GET', f'/sash/api/v1/notebooks/{notebook_id()}/sources')
    if resp.status_code != 200:
        raise QMindError(f'列源返回 {resp.status_code}')
    for s in resp.json().get('sources') or []:
        if (s.get('title') or '') == title:
            return s.get('id')
    return None


def sync_article(title, content, old_source_id=''):
    """上传/更新一个源，返回 source_id

    同标题但内容变化时，服务端会并存两个源——所以先删旧源再传；
    409（内容 SHA 与已有源相同）按去重处理：反查标题拿回 id 即可。
    """
    if old_source_id:
        try:
            delete_source(old_source_id)
        except Exception as e:
            # 删旧失败不阻断新传；顶多短暂并存一份旧内容
            logger.warning(f'QMind 删旧源 {old_source_id} 失败: {e}')
    resp = _request(
        'POST', f'/sash/api/v1/notebooks/{notebook_id()}/sources/upload',
        data={'title': title, 'retain_original_file': 'false'},
        files={'file': (f'{title}.md', content.encode('utf-8'), 'text/markdown')},
    )
    if resp.status_code == 409:
        # 内容未变的重复上传（上次同步成功但本地状态没落库等场景）
        existing = _find_source_id_by_title(title)
        if existing:
            return existing
        raise QMindError(f'上传 409 但按标题「{title}」反查不到源')
    if resp.status_code != 200:
        raise QMindError(f'上传返回 {resp.status_code}: {resp.text[:120]}')
    source_id = resp.json().get('id')
    if not source_id:
        raise QMindError('上传响应缺少 id 字段')
    return source_id


def delete_source(source_id):
    resp = _request('DELETE', f'/sash/api/v1/notebooks/{notebook_id()}/sources/{source_id}')
    if resp.status_code not in (200, 404):
        raise QMindError(f'删除源 {source_id} 返回 {resp.status_code}')
