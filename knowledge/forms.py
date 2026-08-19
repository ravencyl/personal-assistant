from django import forms
from django.utils.text import slugify

from .models import KnowledgeArticle, KnowledgeCategory

INPUT_CLS = 'w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-indigo-500 focus:outline-none'


class ArticleForm(forms.ModelForm):
    new_category = forms.CharField(
        label='新建分类',
        required=False,
        widget=forms.TextInput(attrs={
            'class': INPUT_CLS,
            'placeholder': '输入新分类名称（优先于上方分类选择）',
        }),
    )

    class Meta:
        model = KnowledgeArticle
        fields = ['title', 'category', 'body', 'tags', 'is_published']
        widgets = {
            'title': forms.TextInput(attrs={'class': INPUT_CLS, 'placeholder': '文章标题'}),
            'category': forms.Select(attrs={'class': INPUT_CLS}),
            'body': forms.Textarea(attrs={
                'class': INPUT_CLS + ' font-mono',
                'rows': 12,
                'placeholder': '正文内容，支持 Markdown 格式',
            }),
            'tags': forms.TextInput(attrs={'class': INPUT_CLS, 'placeholder': '多个标签用逗号分隔，如：django,笔记'}),
            'is_published': forms.CheckboxInput(attrs={'class': 'h-4 w-4 rounded border-gray-300 text-indigo-600'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['category'].required = False
        self.fields['category'].empty_label = '未分类'

    def clean(self):
        cleaned = super().clean()
        # 新建分类优先于分类下拉选择
        name = (cleaned.get('new_category') or '').strip()
        if name:
            slug = slugify(name, allow_unicode=True) or slugify(f'category-{KnowledgeCategory.objects.count() + 1}')
            category, _ = KnowledgeCategory.objects.get_or_create(name=name, defaults={'slug': slug})
            cleaned['category'] = category
        return cleaned
