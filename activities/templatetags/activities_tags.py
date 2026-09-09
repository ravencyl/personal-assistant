"""activities 模板标签：费用类别等数据驱动选项的模板侧出口

独立成 activities 的 tag 库而不是塞进 core_tags：类别读取层在 activities
包内，core 不反向依赖业务 app。
"""
from django import template

from activities.categories import active_category_choices

register = template.Library()


@register.simple_tag
def active_expense_categories():
    """启用的费用类别 [(key, label), ...]，供表单下拉循环渲染。

    缓存在 categories.category_rows 内部（5 分钟 TTL + admin 变更即时失效），
    每次页面渲染的开销只是一次缓存读取。
    """
    return active_category_choices()
