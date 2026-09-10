"""备忘录页测试

目前只有桌面两列布局回归锁：本页改造前是整宽单列，改造后右列放筛选与搜索。
「快速记录」按口径留在两列区之上（多字段表单压进 320px 栏会很难用），所以它
不在任何一列切片里，只用移动端顺序锚点断言它仍在列表之前。
"""
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from core.tags import apply_tags
from django.test import TestCase, Client

from notes.models import Note
from core.layout_asserts import assert_desktop_two_columns

User = get_user_model()


class NoteListDesktopLayoutTest(TestCase):
    """备忘录页桌面两列布局回归锁（右列 = 标签筛选 + 搜索）"""
    TEMPLATE = Path(settings.BASE_DIR) / 'templates' / 'notes' / 'note_list.html'

    def setUp(self):
        self.user = User.objects.create_user('raven', password='test')
        self.client = Client()
        self.client.login(username='raven', password='test')
        note = Note.objects.create(user=self.user, content='新西兰南岛自驾路线草稿')
        apply_tags(note, ['自驾'])
        self.html = self.client.get('/notes/').content.decode()

    def test_desktop_two_columns(self):
        assert_desktop_two_columns(
            self, self.html, template_src=self.TEMPLATE.read_text(encoding='utf-8'),
            left=[('新西兰南岛自驾路线草稿', '笔记卡内容')],
            right=[('搜索备忘录...', '搜索框'), ('id="note-tag-filter"', '标签筛选栏')],
            mobile_order=['记点什么...', '搜索备忘录...', '新西兰南岛自驾路线草稿'],
            rail_first=True)

    def test_quick_create_stays_full_width_above_columns(self):
        """速记框（含语音按钮）留在两列区之上：它是本页主操作，也是多字段表单"""
        html = self.html
        self.assertLess(html.index('记点什么...'), html.index('class="page-cols'),
                        '速记框被搬进列容器了，320px 右列里会很难用')


class NoteListRedesignTest(TestCase):
    """备忘列表重设计回归锁：长文折叠钩子 + 置顶视觉 + 分端结构（2026-09）

    折叠本身是纯前端行为（JS 检测内容超高后才加 .is-collapsed），
    这里锁模板钩子与 CSS 组件不被误删。
    """

    def setUp(self):
        self.user = User.objects.create_user('raven', password='test')
        self.client = Client()
        self.client.login(username='raven', password='test')

    def test_fold_hooks_and_length(self):
        """每条备忘渲染折叠钩子，切换按钮带字数（供「展开全文（N 字）」文案）"""
        Note.objects.create(user=self.user, content='短备忘')
        Note.objects.create(user=self.user, content='长' * 500)
        html = self.client.get('/notes/').content.decode()
        # 折叠钩子是无值布尔属性，改数「按钮 + 字数」组合串（JS 选择器不含它）
        self.assertEqual(html.count('data-note-toggle data-length='), 2)
        self.assertIn('data-length="500"', html)

    def test_pinned_note_marked(self):
        Note.objects.create(user=self.user, content='置顶备忘', pinned=True)
        html = self.client.get('/notes/').content.decode()
        self.assertIn('note-item--pinned', html)

    def test_note_item_css_exists(self):
        """custom.css 提供 .note-list / .note-item / 折叠与桌面行式列表样式"""
        css = (Path(settings.BASE_DIR) / 'static' / 'css' / 'custom.css') \
            .read_text(encoding='utf-8')
        for cls in ['.note-list', '.note-item', '.note-fold.is-collapsed',
                    '.note-item--pinned']:
            self.assertIn(cls, css)


class NoteTagEditRegressionTest(TestCase):
    """编辑备忘带标签的回归锁（与活动/知识库同族：_save_m2m 曾拿字符串炸）"""

    def setUp(self):
        self.user = User.objects.create_user('testuser', password='test')
        self.note = Note.objects.create(user=self.user, content='回归备忘')
        apply_tags(self.note, ['旧标签'])

    def test_edit_with_tags_persists(self):
        self.client.force_login(self.user)
        resp = self.client.post(f'/notes/{self.note.id}/edit/', {
            'content': '回归备忘改', 'pinned': '', 'tags': '新标签'})
        self.assertIn(resp.status_code, (200, 302))
        self.note.refresh_from_db()
        self.assertEqual(set(self.note.tags.values_list('name', flat=True)),
                         {'新标签'})
