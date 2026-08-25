from django import forms
from .models import Note


class NoteForm(forms.ModelForm):
    class Meta:
        model = Note
        fields = ['content', 'tags', 'pinned']
        widgets = {
            'content': forms.Textarea(attrs={
                'rows': 4,
                'placeholder': '记点什么...',
                'class': 'w-full rounded-xl border border-[var(--border-strong)] bg-[var(--bg)] text-[var(--text)] px-4 py-3 text-sm focus:border-[var(--accent)] focus:outline-none',
            }),
        }
