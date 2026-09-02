"""知识库 Agent 工具测试

重点是 knowledge.create：让 AI 在对话里把讨论出来的结论直接沉淀成文章。
约定：正文由模型自己根据当前会话整理，服务端只负责落库 + 回链接。
"""
from django.conf import settings
from django.contrib.auth.models import User
from django.test import TestCase, Client
from pathlib import Path

from activities.models import Activity
from notes.models import Note
from core.layout_asserts import assert_desktop_two_columns, code_only

from core.agent_registry import ToolError, get_tool
from core.utils import visible_qs

from .models import Article


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
        self.assertEqual(set(article.tags.names()), {'出差', '签证'})
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
        self.assertEqual(set(article.tags.names()), {'签证', 'EVUS', '面签'})

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
        article.tags.add('亲子')
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
        self.article.tags.add('自驾')
        # 「相关活动与笔记」是条件块，没有共同标签就整块不渲染，顺序锁会空跑
        trip = Activity.objects.create(user=self.user, name='新西兰之旅')
        trip.tags.add('自驾')
        note = Note.objects.create(user=self.user, content='新西兰南岛自驾路线草稿')
        note.tags.add('自驾')
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
