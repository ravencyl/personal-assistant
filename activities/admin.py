from django.contrib import admin
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

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        return qs.filter(user=request.user)


ActivityAdmin.inlines = [ExpenseInline]


@admin.register(Expense)
class ExpenseAdmin(admin.ModelAdmin):
    list_display = ['amount', 'category', 'activity', 'paid_at', 'note', 'created_at']
    list_filter = ['category']
    search_fields = ['note', 'activity__name']
    readonly_fields = ['user', 'created_at', 'updated_at']

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        return qs.filter(user=request.user)

    def save_model(self, request, obj, form, change):
        if not change:
            obj.user = request.user
        super().save_model(request, obj, form, change)
