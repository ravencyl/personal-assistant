"""备忘录页测试

目前只有桌面两列布局回归锁：本页改造前是整宽单列，改造后右列放筛选与搜索。
「快速记录」按口径留在两列区之上（多字段表单压进 320px 栏会很难用），所以它
不在任何一列切片里，只用移动端顺序锚点断言它仍在列表之前。
"""
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
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
        note.tags.add('自驾')
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
