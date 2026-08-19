from django import forms
from .models import Bookmark


class BookmarkForm(forms.ModelForm):
    class Meta:
        model = Bookmark
        fields = ['url', 'title', 'description', 'category']
        widgets = {
            'url': forms.URLInput(attrs={
                'class': 'w-full rounded-md border border-gray-300 px-3 py-2 text-sm',
                'placeholder': 'https://...'
            }),
            'title': forms.TextInput(attrs={
                'class': 'w-full rounded-md border border-gray-300 px-3 py-2 text-sm',
                'placeholder': '标题'
            }),
            'description': forms.Textarea(attrs={
                'class': 'w-full rounded-md border border-gray-300 px-3 py-2 text-sm',
                'rows': 2,
                'placeholder': '备注（可选）'
            }),
            'category': forms.Select(attrs={
                'class': 'rounded-md border border-gray-300 px-3 py-2 text-sm',
            }),
        }
