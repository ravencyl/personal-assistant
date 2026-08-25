from django import forms
from .models import Article


class ArticleForm(forms.ModelForm):
    class Meta:
        model = Article
        fields = ['title', 'content', 'tags']
        widgets = {
            'title': forms.TextInput(attrs={
                'class': 'w-full rounded-xl border border-[var(--border-strong)] bg-[var(--bg)] text-[var(--text)] px-4 py-2.5 text-sm focus:border-[var(--accent)] focus:outline-none',
                'placeholder': '文章标题',
            }),
            'content': forms.Textarea(attrs={
                'rows': 16,
                'class': 'w-full rounded-xl border border-[var(--border-strong)] bg-[var(--bg)] text-[var(--text)] px-4 py-3 text-sm font-mono focus:border-[var(--accent)] focus:outline-none',
                'placeholder': '用 Markdown 写下你的知识...',
            }),
        }
