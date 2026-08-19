from django.shortcuts import get_object_or_404


def visible_qs(model, user):
    """数据可见性规则：超级用户可见全部数据，普通用户仅可见自己的"""
    qs = model.objects.all()
    if user.is_superuser:
        return qs
    return qs.filter(user=user)


def get_visible(model, user, **kwargs):
    """按可见性规则取单对象，不存在或无权时 404"""
    return get_object_or_404(visible_qs(model, user), **kwargs)
