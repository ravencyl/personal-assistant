from django import forms

from core.forms import PlainTagField, PlainTagFormMixin
from .models import Document


class ArticleForm(PlainTagFormMixin, forms.ModelForm):
    class Meta:
        model = Document
        fields = ['title', 'content', 'tags']
        widgets = {
            'title': forms.TextInput(attrs={
                'class': 'tf-underline w-full px-0 py-2.5 text-sm text-[var(--text)] focus:outline-none',
                'placeholder': '文章标题',
            }),
            'content': forms.Textarea(attrs={
                'rows': 16,
                'class': 'tf-underline w-full px-0 py-3 text-sm font-mono text-[var(--text)] focus:outline-none',
                'placeholder': '用 Markdown 写下你的知识...',
            }),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # M2M 自动 formfield 是多选控件，统一为逗号分隔文本输入（与活动/备忘口径一致）；
        # 落库由视图层 apply_tags 完成
        self.fields['tags'] = PlainTagField(label='标签', required=False)
