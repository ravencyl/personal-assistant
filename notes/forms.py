from django import forms

from core.forms import PlainTagField, PlainTagFormMixin
from .models import Note


class NoteForm(PlainTagFormMixin, forms.ModelForm):
    class Meta:
        model = Note
        fields = ['content', 'tags', 'pinned']
        widgets = {
            'content': forms.Textarea(attrs={
                'rows': 4,
                'placeholder': '记点什么...',
                'class': 'w-full rounded-xl border border-[var(--border-strong)] bg-transparent text-[var(--text)] placeholder:text-[var(--text-muted)] px-3.5 py-2.5 text-sm focus:border-[var(--accent)] focus:outline-none',
            }),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # M2M 自动 formfield 是多选控件，统一为逗号分隔文本输入；落库由视图层 apply_tags 完成
        self.fields['tags'] = PlainTagField(label='标签', required=False)
