from django import forms
from .models import Task


class TaskForm(forms.ModelForm):
    class Meta:
        model = Task
        fields = ['title', 'description', 'priority', 'status', 'due_date', 'project', 'activity']
        widgets = {
            'title': forms.TextInput(attrs={
                'class': 'w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-indigo-500 focus:outline-none',
                'placeholder': '任务标题'
            }),
            'description': forms.Textarea(attrs={
                'class': 'w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-indigo-500 focus:outline-none',
                'rows': 3,
                'placeholder': '任务描述（可选）'
            }),
            'priority': forms.Select(attrs={
                'class': 'rounded-md border border-gray-300 px-3 py-2 text-sm',
            }),
            'status': forms.Select(attrs={
                'class': 'rounded-md border border-gray-300 px-3 py-2 text-sm',
            }),
            'due_date': forms.DateTimeInput(attrs={
                'class': 'rounded-md border border-gray-300 px-3 py-2 text-sm',
                'type': 'datetime-local',
            }),
            'project': forms.TextInput(attrs={
                'class': 'w-full rounded-md border border-gray-300 px-3 py-2 text-sm',
                'placeholder': '所属项目（可选）'
            }),
            'activity': forms.Select(attrs={
                'class': 'rounded-md border border-gray-300 px-3 py-2 text-sm',
            }),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        # 所属活动下拉只显示当前用户的活动
        if user is not None:
            from activities.models import Activity
            self.fields['activity'].queryset = Activity.objects.filter(user=user)
        self.fields['activity'].required = False
        self.fields['activity'].empty_label = '无'
