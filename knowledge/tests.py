"""知识库 Agent 工具测试

重点是 knowledge.create：让 AI 在对话里把讨论出来的结论直接沉淀成文章。
约定：正文由模型自己根据当前会话整理，服务端只负责落库 + 回链接。
"""
import json
from pathlib import Path
from unittest.mock import Mock, patch

from django.conf import settings
from django.contrib.auth.models import User
from core.tags import apply_tags
from django.core.cache import cache
from django.test import TestCase, Client, override_settings

from activities.models import Activity
from notes.models import Note
from core.layout_asserts import assert_desktop_two_columns, code_only

from core.agent_registry import ToolError, get_tool
from core.utils import visible_qs

from . import qmind
from .models import Article
from .retrieval import search_knowledge


def _create(user, params):
    return get_tool('knowledge.create')['fn'](user, params)


class KnowledgeCreateAgentToolTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('testuser', password='test')
        self.content = '# 美国 B 签面签准备\n\n- 诚信服务费 250 美元\n- DS-160 确认页\n- EVUS 登记'

    def test_creates_article_owned_by_request_user(self):
        result = _create(self.user, {'title': '美国出差准备清单', 'content': self.content,
                                     'tags': ['出差', '签证']})
        article = Article.objects.get(title='美国出差准备清单')
        self.assertEqual(article.user, self.user)
        self.assertEqual(set(article.tags.values_list('name', flat=True)), {'出差', '签证'})
        self.assertTrue(result['changed'])
        # 回复里要带可读、可点的文章链接（不能被百分号编码成一串乱码）
        self.assertIn(f'/knowledge/{article.slug}/', result['reply'])

    def test_reply_link_actually_opens_the_article(self):
        _create(self.user, {'title': '能打开的文章', 'content': self.content})
        article = Article.objects.get(title='能打开的文章')
        self.client.login(username='testuser', password='test')
        # 中文 slug 未转码直接访问也要能路由（knowledge/urls.py 用 <str:slug>）
        self.assertEqual(self.client.get(f'/knowledge/{article.slug}/').status_code, 200)

    def test_slug_is_unique_when_title_repeats(self):
        """同名文章不能相互覆盖（slug 全局唯一，中文标题走 allow_unicode）"""
        _create(self.user, {'title': '同名文章', 'content': self.content})
        _create(self.user, {'title': '同名文章', 'content': self.content})

        slugs = list(Article.objects.filter(title='同名文章')
                     .order_by('id').values_list('slug', flat=True))
        self.assertEqual(len(slugs), 2)
        self.assertEqual(len(set(slugs)), 2)

    def test_string_tags_split_on_full_width_comma(self):
        """模型很爱用中文顿号/全角逗号回传标签，不能整串当成一个标签"""
        _create(self.user, {'title': '标签拆分', 'content': self.content,
                            'tags': '签证、EVUS，面签，EVUS'})
        article = Article.objects.get(title='标签拆分')
        self.assertEqual(set(article.tags.values_list('name', flat=True)), {'签证', 'EVUS', '面签'})

    def test_missing_title_or_content_raises_tool_error(self):
        with self.assertRaises(ToolError):
            _create(self.user, {'content': self.content})
        with self.assertRaises(ToolError):
            _create(self.user, {'title': '只有标题'})

    def test_stub_content_is_rejected(self):
        """正文太短说明模型没把上下文展开，存进去也查不出来，直接让它补"""
        with self.assertRaises(ToolError):
            _create(self.user, {'title': '随手一句', 'content': '就这几个字'})

    def test_created_article_is_searchable_by_owner_only(self):
        other = User.objects.create_user('other', password='test')
        _create(self.user, {'title': '我的面签结论', 'content': self.content})

        self.assertTrue(visible_qs(Article, self.user).filter(title='我的面签结论').exists())
        self.assertFalse(visible_qs(Article, other).filter(title='我的面签结论').exists())

    def test_created_article_is_reachable_by_search_tool(self):
        """存进去要能再被 knowledge.search 查出来，否则沉淀没有闭环"""
        _create(self.user, {'title': '美国面签结论', 'content': self.content})
        result = get_tool('knowledge.search')['fn'](self.user, {'keyword': 'EVUS'})
        self.assertIn('美国面签结论', result['reply'])


def _update(user, params):
    return get_tool('knowledge.update')['fn'](user, params)


class KnowledgeUpdateAgentToolTest(TestCase):
    """knowledge.update：让 AI 在对话里直接修订已有文章（append 为主，replace 需显式）

    风险点不在「能不能改」而在「改到哪一篇 / 改成什么」：目标必须唯一，
    整段替换不能是一小句（多半是模型没展开上下文）。
    """

    def setUp(self):
        self.user = User.objects.create_user('testuser', password='test')
        self.article = Article.objects.create(
            user=self.user, title='桐庐龙井峡行程',
            content='# 行程\n\n第一天：龙井峡漂流')

    def test_append_adds_content_and_keeps_original(self):
        original = self.article.content
        result = _update(self.user, {'target': '桐庐', 'content': '第二天：芦茨村慢生活'})
        self.article.refresh_from_db()
        self.assertIn(original, self.article.content)
        self.assertIn('第二天：芦茨村慢生活', self.article.content)
        self.assertTrue(result['changed'])
        self.assertIn(f'/knowledge/{self.article.slug}/', result['reply'])

    def test_replace_needs_explicit_mode_and_full_content(self):
        _update(self.user, {'target': '桐庐', 'content': '# 全新行程\n\n只去漂流，不去村庄',
                            'content_mode': 'replace'})
        self.article.refresh_from_db()
        self.assertNotIn('芦茨村', self.article.content)
        self.assertTrue(self.article.content.startswith('# 全新行程'))

    def test_replace_with_stub_content_is_rejected(self):
        before = self.article.content
        with self.assertRaises(ToolError):
            _update(self.user, {'target': '桐庐', 'content': '改好了',
                                'content_mode': 'replace'})
        self.article.refresh_from_db()
        self.assertEqual(self.article.content, before, '整段替换被拒后原文不能被动')

    def test_unresolved_last_reply_ref_is_rejected(self):
        before = self.article.content
        with self.assertRaises(ToolError):
            _update(self.user, {'target': '桐庐', 'content': '$LAST_REPLY'})
        self.article.refresh_from_db()
        self.assertNotIn('$LAST_REPLY', self.article.content,
                         '裸引用标记绝不能被当成正文写进文章')

    def test_target_must_be_unique(self):
        Article.objects.create(user=self.user, title='桐庐芦茨村攻略', content='x' * 20)
        with self.assertRaises(ToolError) as ctx:
            _update(self.user, {'target': '桐庐', 'content': '补充一段' * 5})
        self.assertTrue(hasattr(ctx.exception, 'candidates'),
                        '目标不唯一必须携带候选列表供用户辨认')

    def test_cannot_touch_other_users_articles(self):
        other = User.objects.create_user('other', password='test')
        before = self.article.content
        with self.assertRaises(ToolError):
            _update(other, {'target': '桐庐', 'content': '隔壁用户的恶意补充' * 3})
        self.article.refresh_from_db()
        self.assertEqual(self.article.content, before)

    def test_missing_target_or_change_raises_tool_error(self):
        with self.assertRaises(ToolError):
            _update(self.user, {'content': '没有目标也改不了'})
        with self.assertRaises(ToolError):
            _update(self.user, {'target': '桐庐'})  # 没说改什么

    def test_no_change_returns_changed_false(self):
        result = _update(self.user, {'target': '桐庐', 'title': '桐庐龙井峡行程'})
        self.assertFalse(result['changed'])

    def test_tags_are_merged_not_replaced(self):
        apply_tags(self.article, ['漂流'])
        _update(self.user, {'target': '桐庐', 'tags': ['漂流', '周末游']})
        self.article.refresh_from_db()
        self.assertEqual(set(self.article.tags.values_list('name', flat=True)), {'漂流', '周末游'})

    def test_retitle_keeps_slug_stable(self):
        """改标题不能换 slug，否则对话里刚发出去的链接立刻失效"""
        old_slug = self.article.slug
        _update(self.user, {'target': '桐庐', 'title': '桐庐龙井峡完整行程'})
        self.article.refresh_from_db()
        self.assertEqual(self.article.slug, old_slug)
        self.assertEqual(self.article.title, '桐庐龙井峡完整行程')


class ArticleListDesktopLayoutTest(TestCase):
    """知识库列表页桌面两列布局回归锁（右列 = 搜索 + 标签筛选）

    本页原来套 max-w-4xl 居中，桌面端右侧白掉约 320px。rail-first 保证移动端
    顺序（搜索 → 标签 → 列表）与改造前一致。
    """
    TEMPLATE = Path(settings.BASE_DIR) / 'templates' / 'knowledge' / 'article_list.html'

    def setUp(self):
        self.user = User.objects.create_user('raven', password='test')
        self.client = Client()
        self.client.login(username='raven', password='test')
        article = Article.objects.create(user=self.user, title='桐庐周末游',
                                         content='# 行程\n龙井峡漂流')
        apply_tags(article, ['亲子'])
        self.html = self.client.get('/knowledge/').content.decode()

    def test_desktop_two_columns(self):
        assert_desktop_two_columns(
            self, self.html, template_src=self.TEMPLATE.read_text(encoding='utf-8'),
            left=[('桐庐周末游', '文章卡标题'), ('前更新', '文章卡时间')],
            right=[('搜索文章...', '搜索框'), ('id="article-tag-filter"', '标签筛选栏')],
            mobile_order=['搜索文章...', '桐庐周末游'],
            rail_first=True)

    def test_no_legacy_centering_container(self):
        """旧的 max-w-4xl 居中壳必须去掉：套两层会把左列挤窄，两列口径就白做

        扫的是 code_only(src)：上面那段「为什么改」的注释里就写了 max-w-4xl，
        不剔注释就是在拿散文当代码。"""
        self.assertNotIn('max-w-4xl', code_only(self.TEMPLATE.read_text(encoding='utf-8')),
                         '页面级居中壳还在，列宽被外层限制')


class ArticleDetailDesktopLayoutTest(TestCase):
    """文章详情页桌面两列布局回归锁（正文在左 · 关联与操作在右）

    两列化后左列有 864px，正文必须自己限宽：阅读行长不跟着列宽跑。
    页面上下文属性 data-page-context 必须仍在包住两列区的外层元素上。
    """
    TEMPLATE = Path(settings.BASE_DIR) / 'templates' / 'knowledge' / 'article_detail.html'

    def setUp(self):
        self.user = User.objects.create_user('raven', password='test')
        self.client = Client()
        self.client.login(username='raven', password='test')
        self.article = Article.objects.create(user=self.user, title='桐庐周末游',
                                              content='# 行程\n\n龙井峡漂流，记得带泳衣。')
        apply_tags(self.article, ['自驾'])
        # 「相关活动与笔记」是条件块，没有共同标签就整块不渲染，顺序锁会空跑
        trip = Activity.objects.create(user=self.user, name='新西兰之旅')
        apply_tags(trip, ['自驾'])
        note = Note.objects.create(user=self.user, content='新西兰南岛自驾路线草稿')
        apply_tags(note, ['自驾'])
        self.html = self.client.get(f'/knowledge/{self.article.slug}/').content.decode()

    def test_desktop_two_columns(self):
        assert_desktop_two_columns(
            self, self.html, template_src=self.TEMPLATE.read_text(encoding='utf-8'),
            left=[('markdown-content', '正文区'), ('桐庐周末游', '文章标题')],
            right=[('id="related-content"', '相关活动与笔记'), ('id="article-actions"', '操作卡'),
                   ('编辑', '编辑入口')],
            mobile_order=['markdown-content', 'id="related-content"', 'id="article-actions"'])

    def test_prose_measure_capped_after_widening(self):
        """正文限宽：864px 左列里一行超 100 字就读不动了"""
        at = self.html.index('markdown-content')
        self.assertIn('max-w-2xl', self.html[at - 20:at + 120],
                      '正文容器丢了限宽类')
        self.assertNotIn('max-w-none', self.html[at - 20:at + 120])

    def test_page_context_attr_stays_on_outer_wrapper(self):
        """页面上下文感知靠它：搬进左列会让 JS 的作用域变窄"""
        at = self.html.index('data-page-context="knowledge_detail"')
        self.assertLess(at, self.html.index('class="page-cols'),
                        'data-page-context 应在包住两列区的外层元素上')


class QMindClientTest(TestCase):
    """QMind 客户端：token 缓存、401 重试、同步与 409 去重（HTTP 全 mock，不出网）"""

    def setUp(self):
        cache.delete(qmind.TOKEN_CACHE_KEY)
        # 预置缓存 token，让只测同步逻辑的用例不必给 exchange 打桩
        cache.set(qmind.TOKEN_CACHE_KEY, 'jt-test', 600)

    def _config(self):
        return override_settings(QMIND_NOTEBOOK_ID='nb-1', QODER_ACCESS_TOKEN='pt-x')

    def test_configured_requires_notebook_and_token(self):
        with self._config():
            self.assertTrue(qmind.configured())
        with override_settings(QMIND_NOTEBOOK_ID=''):
            self.assertFalse(qmind.configured())

    def test_job_token_is_cached_across_calls(self):
        cache.delete(qmind.TOKEN_CACHE_KEY)  # setUp 预置了 token，这里从冷启动开始验证 exchange 只发生一次
        with self._config(), \
             patch('knowledge.qmind.httpx.post', return_value=self._resp(200, {'token': 'jt-1', 'expires_in': 86400000})) as m_post, \
             patch('knowledge.qmind.httpx.request', return_value=self._resp(200, {'chunks': []})) as m_req:
            qmind.retrieve('q1')
            qmind.retrieve('q2')
        self.assertEqual(m_post.call_count, 1, '第二次调用应命中缓存，不再 exchange')
        self.assertEqual(m_req.call_count, 2)

    def test_retrieve_401_retries_once_with_fresh_token(self):
        responses = [self._resp(401, {}), self._resp(200, {'chunks': [{'title': 'T', 'snippet': 's', 'score': 0.9}]})]
        with self._config(), \
             patch('knowledge.qmind.httpx.post', return_value=self._resp(200, {'token': 'jt-1', 'expires_in': 86400000})), \
             patch('knowledge.qmind.httpx.request', side_effect=responses) as m_req:
            out = qmind.retrieve('q')
        self.assertEqual(m_req.call_count, 2)
        self.assertEqual(out[0]['title'], 'T')

    def test_retrieve_not_configured_returns_none(self):
        with override_settings(QMIND_NOTEBOOK_ID=''):
            self.assertIsNone(qmind.retrieve('q'))

    def test_sync_article_returns_id_and_deletes_old_first(self):
        calls = []
        with self._config(), \
             patch('knowledge.qmind.httpx.request',
                   side_effect=lambda m, url, **kw: calls.append(m) or self._resp(200, {'id': 'src-new'})):
            sid = qmind.sync_article('T', 'c', old_source_id='src-old')
        self.assertEqual(sid, 'src-new')
        self.assertEqual(calls, ['DELETE', 'POST'], '更新 = 先删旧源再上传')

    def test_sync_article_409_dedup_falls_back_to_title_lookup(self):
        with self._config(), \
             patch('knowledge.qmind.httpx.request', side_effect=[
                 self._resp(409, {'errorCode': 'AlreadyExists'}),
                 self._resp(200, {'sources': [{'id': 'src-dup', 'title': 'T'}]}),
             ]):
            sid = qmind.sync_article('T', 'c')
        self.assertEqual(sid, 'src-dup')

    def test_delete_source_tolerates_404(self):
        with self._config(), \
             patch('knowledge.qmind.httpx.request', return_value=self._resp(404, {})):
            qmind.delete_source('gone')  # 不抛即通过

    @staticmethod
    def _resp(status, data):
        m = Mock()
        m.status_code = status
        m.json.return_value = data
        m.text = json.dumps(data)
        return m


class KnowledgeRetrievalLayerTest(TestCase):
    """统一检索层：QMind 优先、失败降级本地、片段回挂本地文章"""

    def setUp(self):
        self.user = User.objects.create_user(username='kr', password='x')
        self.article = Article.objects.create(user=self.user, title='桐庐行程', content='预算 1200 元，带溯溪鞋')

    def test_not_configured_falls_back_to_local(self):
        with override_settings(QMIND_NOTEBOOK_ID=''):
            hits = search_knowledge(self.user, '桐庐', limit=3)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]['article'], self.article)
        self.assertIn('预算', hits[0]['content'])

    def test_qmind_error_degrades_to_local(self):
        with override_settings(QMIND_NOTEBOOK_ID='nb-1', QODER_ACCESS_TOKEN='pt-x'), \
             patch('knowledge.qmind.retrieve', side_effect=qmind.QMindError('boom')):
            hits = search_knowledge(self.user, '桐庐', limit=3)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]['article'], self.article)

    def test_qmind_chunks_mapped_back_to_local_article(self):
        chunks = [{'title': '桐庐行程', 'snippet': '带防滑溯溪鞋', 'score': 0.8, 'uri': 'notebook/sources/x/桐庐行程.md'}]
        with override_settings(QMIND_NOTEBOOK_ID='nb-1', QODER_ACCESS_TOKEN='pt-x'), \
             patch('knowledge.qmind.retrieve', return_value=chunks):
            hits = search_knowledge(self.user, '溯溪装备要注意什么', limit=3)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]['article'], self.article, '片段应回挂到本地文章获得深链')
        self.assertEqual(hits[0]['content'], '带防滑溯溪鞋')


class ArticleQMindSyncSignalTest(TestCase):
    """信号同步：内容变化才同步、删除清理云端源（同步线程同步化执行以便断言）"""

    def setUp(self):
        self.user = User.objects.create_user(username='ks', password='x')

    def _run_inline(self):
        """把后台线程替换成立即执行，方便断言"""
        class InlineThread:
            def __init__(self, target, args, **kw):
                self._target, self._args = target, args

            def start(self):
                self._target(*self._args)

        return patch('knowledge.qmind_sync.threading.Thread', InlineThread)

    def test_save_syncs_new_article_and_stores_source_id(self):
        with patch('knowledge.qmind_sync._enabled', return_value=True), \
             patch('knowledge.qmind.sync_article', return_value='src-1') as m_sync, \
             self._run_inline():
            article = Article.objects.create(user=self.user, title='新文章', content='正文')
        m_sync.assert_called_once()
        article.refresh_from_db()
        self.assertEqual(article.qmind_source_id, 'src-1')
        self.assertEqual(article.qmind_sync_hash, article.sync_hash())

    def test_unchanged_resave_skips_sync(self):
        with patch('knowledge.qmind_sync._enabled', return_value=True), \
             patch('knowledge.qmind.sync_article', return_value='src-1'), \
             self._run_inline():
            article = Article.objects.create(user=self.user, title='稳文章', content='v1')
        article.refresh_from_db()  # 模拟下一次请求从 DB 读到同步后的状态
        with patch('knowledge.qmind_sync._enabled', return_value=True), \
             patch('knowledge.qmind.sync_article') as m_sync, \
             self._run_inline():
            article.save()
        self.assertFalse(m_sync.called, '指纹未变不应再发同步请求')

    def test_sync_failure_does_not_break_article_save(self):
        with patch('knowledge.qmind_sync._enabled', return_value=True), \
             patch('knowledge.qmind.sync_article', side_effect=qmind.QMindError('网络炸了')), \
             self._run_inline():
            article = Article.objects.create(user=self.user, title='炸文章', content='正文')  # 不抛即通过
        article.refresh_from_db()
        self.assertEqual(article.qmind_source_id, '', '失败时云端状态保持为空，下次保存重试')

    def test_delete_cleans_cloud_source(self):
        with patch('knowledge.qmind_sync._enabled', return_value=True), \
             patch('knowledge.qmind.sync_article', return_value='src-9'), \
             self._run_inline():
            article = Article.objects.create(user=self.user, title='删文章', content='正文')
        article.refresh_from_db()  # 同步在后台完成，删除信号需要拿到落库后的 source_id
        with patch('knowledge.qmind_sync._enabled', return_value=True), \
             patch('knowledge.qmind.delete_source') as m_del, \
             self._run_inline():
            article.delete()
        m_del.assert_called_once_with('src-9')

    def test_disabled_does_nothing(self):
        with patch('knowledge.qmind_sync._enabled', return_value=False), \
             patch('knowledge.qmind.sync_article') as m_sync, \
             self._run_inline():
            Article.objects.create(user=self.user, title='关文章', content='正文')
        m_sync.assert_not_called()


class KnowledgeSearchAgentToolQMindTest(TestCase):
    """knowledge.search 走统一检索层后的行为保持"""

    def setUp(self):
        self.user = User.objects.create_user(username='kt', password='x')
        Article.objects.create(user=self.user, title='独处训练', content='从每天 10 分钟开始练习')

    def test_search_hits_via_qmind_with_deep_link(self):
        chunks = [{'title': '独处训练', 'snippet': '四周阶梯练习', 'score': 0.7, 'uri': ''}]
        with override_settings(QMIND_NOTEBOOK_ID='nb-1', QODER_ACCESS_TOKEN='pt-x'), \
             patch('knowledge.qmind.retrieve', return_value=chunks):
            out = get_tool('knowledge.search')['fn'](self.user, {'keyword': '一个人待着心慌怎么办'})
        self.assertIn('独处训练', out['reply'])
        self.assertIn('knowledge/', out['reply'], '命中本地文章时要给深链')

    def test_search_empty_reply_unchanged(self):
        with override_settings(QMIND_NOTEBOOK_ID='nb-1', QODER_ACCESS_TOKEN='pt-x'), \
             patch('knowledge.qmind.retrieve', return_value=[]):
            out = get_tool('knowledge.search')['fn'](self.user, {'keyword': '不存在的主题'})
        self.assertIn('知识库里没有', out['reply'])


class ArticleTagEditRegressionTest(TestCase):
    """编辑文章带标签的回归锁（PlainTagFormMixin 前 form.save() 曾把
    名字字符串传给 M2M.set 逐字符当主键 → 500；与活动编辑同族）"""

    def setUp(self):
        self.user = User.objects.create_user('testuser', password='test')
        self.article = Article.objects.create(
            user=self.user, title='标签回归文章', content='x')
        apply_tags(self.article, ['旧标签'])

    def test_edit_with_tags_persists(self):
        self.client.force_login(self.user)
        resp = self.client.post(f'/knowledge/{self.article.pk}/edit/', {
            'title': '标签回归文章', 'content': 'y', 'tags': '新标签, 知识'})
        self.assertEqual(resp.status_code, 302)
        self.article.refresh_from_db()
        self.assertEqual(set(self.article.tags.values_list('name', flat=True)),
                         {'新标签', '知识'})
