from django import forms

from core.forms import PlainTagField
from core.utils import visible_qs

from .models import Activity
from .services import record_parsed_cost
from .utils import resolve_participants

INPUT_CLS = 'w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-indigo-500 focus:outline-none'


class ActivityForm(forms.ModelForm):
    participants_input = forms.CharField(
        label='参与者',
        required=False,
        widget=forms.HiddenInput,
    )
    new_children = forms.CharField(
        label='子任务',
        required=False,
        widget=forms.TextInput(attrs={
            'class': INPUT_CLS,
            'placeholder': '多个名称用逗号分隔，将随活动一并创建（可选）',
        }),
        help_text='输入子任务名称，保存时自动创建为子活动',
    )
    # 与「预算上限」区分开：这里是从一句话里识别出的已花金额，保存时记为一笔支出
    parsed_cost = forms.DecimalField(
        label='本次费用',
        required=False,
        max_digits=10,
        decimal_places=2,
        widget=forms.NumberInput(attrs={
            'class': INPUT_CLS,
            'placeholder': '可选，已经花掉的钱',
            'step': '0.01',
            'min': '0',
        }),
        help_text='保存时记为该活动的第一笔支出（区别于上方预算上限）',
    )

    class Meta:
        model = Activity
        fields = ['name', 'description', 'start_date', 'end_date', 'status', 'budget', 'duration_minutes', 'parent', 'tags']
        widgets = {
            'name': forms.TextInput(attrs={'class': INPUT_CLS, 'placeholder': '活动名称'}),
            'description': forms.Textarea(attrs={'class': INPUT_CLS, 'rows': 3, 'placeholder': '活动描述（可选）'}),
            'start_date': forms.DateInput(attrs={'class': INPUT_CLS, 'type': 'date'}, format='%Y-%m-%d'),
            'end_date': forms.DateInput(attrs={'class': INPUT_CLS, 'type': 'date'}, format='%Y-%m-%d'),
            'status': forms.Select(attrs={'class': 'rounded-md border border-gray-300 px-3 py-2 text-sm'}),
            'budget': forms.NumberInput(attrs={'class': INPUT_CLS, 'placeholder': '可选，设置预算上限', 'step': '0.01', 'min': '0'}),
            'duration_minutes': forms.NumberInput(attrs={'class': INPUT_CLS, 'placeholder': '可选，耗时分钟数，如 150', 'step': '1', 'min': '0'}),
            'parent': forms.Select(attrs={'class': INPUT_CLS}),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        # 日期字段均非必填，不设置任何默认值
        self.fields['start_date'].required = False
        self.fields['end_date'].required = False
        self.fields['duration_minutes'].required = False
        # 父活动候选走统一可见性口径（超管不把活动限在自己名下，否则下拉缺项）
        self.fields['parent'].queryset = (
            visible_qs(Activity, user) if user is not None else Activity.objects.none())
        if self.instance.pk:
            self.fields['parent'].queryset = self.fields['parent'].queryset.exclude(pk=self.instance.pk)
            # 编辑时回显已关联的参与者
            self.fields['participants_input'].initial = ', '.join(
                self.instance.participants.values_list('name', flat=True)
            )
        self.fields['parent'].empty_label = '无（顶级活动）'
        # 标签用自定义字段 + 隐藏输入（前端 autocomplete 组件同步逗号分隔值）
        self.fields['tags'] = PlainTagField(
            label='标签',
            required=False,
            widget=forms.HiddenInput,
        )
        # 编辑页不单独记账：详情页已有费用明细区，重复提供入口会造成二次计费
        if self.instance.pk:
            del self.fields['parsed_cost']

    def clean(self):
        cleaned = super().clean()
        start = cleaned.get('start_date')
        end = cleaned.get('end_date')
        if start and end and end < start:
            raise forms.ValidationError('结束日期不能早于开始日期')
        return cleaned

    def save_participants(self, activity):
        """解析参与者输入：先不区分大小写复用已有写法，确实没有才新建，并全量替换关联"""
        raw = self.cleaned_data.get('participants_input', '')
        names = [n.strip() for n in raw.replace('，', ',').split(',') if n.strip()]
        participants, _skipped, _created = resolve_participants(
            self.user, names, create_missing=True)
        activity.participants.set(participants)

    def save_children(self, activity):
        """创建页批量创建子活动（逗号分隔），返回新建子活动列表"""
        raw = self.cleaned_data.get('new_children', '')
        children = []
        for name in [n.strip() for n in raw.replace('，', ',').split(',') if n.strip()]:
            children.append(Activity.objects.create(
                user=self.user,
                name=name,
                parent=activity,
            ))
        return children

    def save_cost(self, activity):
        """把「本次费用」记为一笔支出，返回新建的 Expense（未填则 None）

        编辑页没有这个字段（__init__ 已移除），因此只会在新建时生效。
        """
        return record_parsed_cost(activity, activity.user,
                                  self.cleaned_data.get('parsed_cost'),
                                  note='新建时录入')
