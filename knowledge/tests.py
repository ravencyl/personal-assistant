"""知识库 Agent 工具测试

重点是 knowledge.create：让 AI 在对话里把讨论出来的结论直接沉淀成文章。
约定：正文由模型自己根据当前会话整理，服务端只负责落库 + 回链接。
"""
from django.contrib.auth.models import User
from django.test import TestCase

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
