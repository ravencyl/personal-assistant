from django import forms as dj_forms
from django.contrib import admin
from .categories import active_category_choices, invalidate_category_cache
from .models import Activity, ExpenseCategory, Participant, Expense

class CategoryChoiceField(dj_forms.TypedChoiceField):
    """费用类别下拉（admin 用）：容忍 model CharField.formfield() 透传的
    max_length（ChoiceField 家族没有该参数，直接传会炸 TypeError）"""
    def __init__(self, *args, max_length=None, **kwargs):
        super().__init__(*args, **kwargs)



@admin.register(Participant)
class ParticipantAdmin(admin.ModelAdmin):
    list_display = ['name', 'user', 'note', 'created_at']
    search_fields = ['name', 'note']

    def get_queryset(self, request):
        # 普通用户只能看到自己的参与者
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        return qs.filter(user=request.user)


@admin.register(Activity)
class ActivityAdmin(admin.ModelAdmin):
    list_display = ['name', 'status', 'start_date', 'end_date', 'total_cost', 'parent']
    list_filter = ['status']
    search_fields = ['name', 'description']
    filter_horizontal = ['participants']
    readonly_fields = ['user', 'created_at', 'updated_at']
    date_hierarchy = 'start_date'

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        # 父活动/参与者只能选自己的数据
        if db_field.name == 'parent':
            kwargs['queryset'] = Activity.objects.filter(user=request.user)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def formfield_for_manytomany(self, db_field, request, **kwargs):
        if db_field.name == 'participants':
            kwargs['queryset'] = Participant.objects.filter(user=request.user)
        return super().formfield_for_manytomany(db_field, request, **kwargs)

    def save_model(self, request, obj, form, change):
        if not change:
            obj.user = request.user
        super().save_model(request, obj, form, change)


@admin.register(ExpenseCategory)
class ExpenseCategoryAdmin(admin.ModelAdmin):
    """费用类别管理入口：增/删/改/停用全在这里，各入口零代码同步生效

    建议「停用」而非删除：删除后历史费用的类别列会回落显示 key 本身
    （不报错、不丢数据，但展示不如中文友好）。
    """
    list_display = ['key', 'label', 'sort', 'is_active']
    list_editable = ['sort', 'is_active']
    list_display_links = ['key', 'label']
    ordering = ['sort', 'id']

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        invalidate_category_cache()

    def delete_model(self, request, obj):
        super().delete_model(request, obj)
        invalidate_category_cache()


class ExpenseCategoryFilter(admin.SimpleListFilter):
    """费用列表按类别筛选（动态）：Expense.category 已无 choices，
    原生 list_filter 不再可用，改为从类别表动态取值（含停用，历史可筛）"""

    title = '类别'
    parameter_name = 'category'

    def lookups(self, request, model_admin):
        from .categories import category_label_map
        labels = category_label_map()
        used = set(Expense.objects.values_list('category', flat=True).distinct())
        # 只列真实用到的类别，避免下拉越拉越长；新启用的类别记一笔后自动出现
        return [(key, labels.get(key, key)) for key in sorted(used)]

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(category=self.value())


class ExpenseInline(admin.TabularInline):
    model = Expense
    extra = 0
    readonly_fields = ['created_at']

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        if db_field.name == 'category':
            # category 是无 choices 的 CharField：手动传 choices 会落到 CharField
            # （不接受该参数）而炸，必须显式指定 ChoiceField 类
            kwargs['form_class'] = CategoryChoiceField
            kwargs['choices'] = active_category_choices()
            kwargs['required'] = False
            kwargs['empty_value'] = ''
        return super().formfield_for_dbfield(db_field, request, **kwargs)

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        return qs.filter(user=request.user)


ActivityAdmin.inlines = [ExpenseInline]


@admin.register(Expense)
class ExpenseAdmin(admin.ModelAdmin):
    list_display = ['amount', 'category', 'activity', 'paid_at', 'note', 'created_at']
    list_filter = [ExpenseCategoryFilter]
    search_fields = ['note', 'activity__name']
    readonly_fields = ['user', 'created_at', 'updated_at']

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        if db_field.name == 'category':
            choices = list(active_category_choices())
            # 编辑历史费用时当前值可能是停用类别：option 必须在，否则保存即被改 写
            obj_id = request.resolver_match.kwargs.get('object_id') if request.resolver_match else None
            if obj_id:
                current = Expense.objects.filter(pk=obj_id).values_list('category', flat=True).first()
                if current and current not in {k for k, _ in choices}:
                    choices.append((current, current))
            # 同 ExpenseInline：CharField 不接 choices，需显式 ChoiceField
            kwargs['form_class'] = CategoryChoiceField
            kwargs['choices'] = choices
            kwargs['required'] = False
            kwargs['empty_value'] = ''
        return super().formfield_for_dbfield(db_field, request, **kwargs)

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        return qs.filter(user=request.user)

    def save_model(self, request, obj, form, change):
        if not change:
            obj.user = request.user
        super().save_model(request, obj, form, change)
