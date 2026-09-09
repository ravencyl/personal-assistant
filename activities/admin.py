from django.contrib import admin

from core.models import Tag

from .models import Activity, Participant, Expense


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


class ExpenseInline(admin.TabularInline):
    model = Expense
    extra = 0
    readonly_fields = ['created_at']
    filter_horizontal = ['tags']

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        return qs.filter(user=request.user)


ActivityAdmin.inlines = [ExpenseInline]


class ExpenseTagFilter(admin.SimpleListFilter):
    """费用列表按标签筛选（动态）：只列真实用到的 expense scope 标签，
    避免下拉越拉越长；给某笔费用打上新标签后自动出现"""

    title = '标签'
    parameter_name = 'tag'

    def lookups(self, request, model_admin):
        used = (Tag.objects.filter(expense__isnull=False)
                .values_list('name', flat=True).distinct())
        return [(name, name) for name in sorted(used)]

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(tags__name=self.value())


@admin.register(Expense)
class ExpenseAdmin(admin.ModelAdmin):
    list_display = ['amount', 'activity', 'paid_at', 'note', 'tag_list', 'created_at']
    list_filter = [ExpenseTagFilter]
    search_fields = ['note', 'activity__name', 'tags__name']
    readonly_fields = ['user', 'created_at', 'updated_at']
    filter_horizontal = ['tags']

    @admin.display(description='标签')
    def tag_list(self, obj):
        # M2M 不能直接进 list_display，用方法列（每行一次小查询，
        # admin 列表页已分页，量级可忽略）
        return ', '.join(obj.tags.values_list('name', flat=True))

    def formfield_for_manytomany(self, db_field, request, **kwargs):
        if db_field.name == 'tags':
            # 编辑页标签可选列表只列 expense scope（费用归费用、活动归活动）
            kwargs['queryset'] = Tag.objects.filter(scope='expense')
        return super().formfield_for_manytomany(db_field, request, **kwargs)

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        return qs.filter(user=request.user)

    def save_model(self, request, obj, form, change):
        if not change:
            obj.user = request.user
        super().save_model(request, obj, form, change)
