from django import forms
from django.utils import timezone

from .models import Activity, Participant

INPUT_CLS = 'w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-indigo-500 focus:outline-none'


class ActivityForm(forms.ModelForm):
    new_participants = forms.CharField(
        label='新增参与者',
        required=False,
        widget=forms.TextInput(attrs={
            'class': INPUT_CLS,
            'placeholder': '多个姓名用逗号分隔，如：张三,李四',
        }),
        help_text='输入新参与者姓名，保存时自动创建',
    )

    class Meta:
        model = Activity
        fields = ['name', 'description', 'start_date', 'end_date', 'status', 'cost', 'parent', 'participants']
        widgets = {
            'name': forms.TextInput(attrs={'class': INPUT_CLS, 'placeholder': '活动名称'}),
            'description': forms.Textarea(attrs={'class': INPUT_CLS, 'rows': 3, 'placeholder': '活动描述（可选）'}),
            'start_date': forms.DateInput(attrs={'class': INPUT_CLS, 'type': 'date'}, format='%Y-%m-%d'),
            'end_date': forms.DateInput(attrs={'class': INPUT_CLS, 'type': 'date'}, format='%Y-%m-%d'),
            'status': forms.Select(attrs={'class': 'rounded-md border border-gray-300 px-3 py-2 text-sm'}),
            'cost': forms.NumberInput(attrs={'class': INPUT_CLS, 'step': '0.01', 'min': '0'}),
            'parent': forms.Select(attrs={'class': INPUT_CLS}),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        # 开始日期非必填；结束日期默认当天
        self.fields['start_date'].required = False
        self.fields['end_date'].required = False
        if self.instance.pk is None:
            self.fields['end_date'].initial = timezone.localdate()
        # 父活动和参与者只能选自己的数据
        self.fields['parent'].queryset = Activity.objects.filter(user=user)
        if self.instance.pk:
            self.fields['parent'].queryset = self.fields['parent'].queryset.exclude(pk=self.instance.pk)
        self.fields['parent'].empty_label = '无（顶级活动）'
        self.fields['participants'] = forms.ModelMultipleChoiceField(
            label='参与者',
            required=False,
            queryset=Participant.objects.filter(user=user),
            widget=forms.CheckboxSelectMultiple,
        )

    def clean(self):
        cleaned = super().clean()
        start = cleaned.get('start_date')
        end = cleaned.get('end_date')
        if start and end and end < start:
            raise forms.ValidationError('结束日期不能早于开始日期')
        return cleaned

    def save_participants(self, activity):
        """处理新增参与者文本：创建并关联到活动"""
        raw = self.cleaned_data.get('new_participants', '')
        for name in [n.strip() for n in raw.replace('，', ',').split(',') if n.strip()]:
            p, _ = Participant.objects.get_or_create(user=self.user, name=name)
            activity.participants.add(p)
