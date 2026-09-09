from django import forms


class PlainTagField(forms.CharField):
    """标签表单字段：逗号分隔文本 ↔ 对象标签互转，配合普通 TextInput。

    2026-09 标签体系自建（core.Tag M2M）后，ModelForm 不再自动生成
    taggit 的 TagField——多选控件会改变交互，这里统一为文本输入：
    - prepare_value：M2M manager / queryset / 实例列表 → 'a, b, c'（编辑回显）
    - 渲染后是普通文本框，提交原样是字符串，由视图层 core.tags.split_tag_names
      清洗后 apply_tags / add_tags 落库
    """

    def prepare_value(self, value):
        if value is None:
            return ''
        if isinstance(value, str):
            return value
        # M2M manager / queryset / 实例列表统一走 name 提取
        if hasattr(value, 'all'):
            value = value.all()
        names = []
        for tag in value:
            names.append(getattr(tag, 'name', str(tag)))
        return ', '.join(names)


class PlainTagFormMixin:
    """用 PlainTagField 覆盖模型 tags M2M 字段的 ModelForm 必须混入。

    ModelForm._save_m2m 只认模型 M2M 字段名（不看表单字段类型）：
    form.save() / form.save_m2m() 会把 cleaned_data['tags']——名字字符串——
    直接传给 instance.tags.set()，M2M 把字符串逐字符当主键查询而炸
    （实测：ValueError: Field 'id' expected a number but got '新'）。
    本 mixin 在 _save_m2m 前摘掉 tags 键让 Django 跳过，随后原样放回，
    tags 落库仍由视图层 apply_tags 单一入口完成。

    实测踩过（2026-09）：活动编辑页提交带标签 → 500。
    """

    def _save_m2m(self):
        cleaned = self.cleaned_data
        tags_value = cleaned.pop('tags', None)
        try:
            super()._save_m2m()
        finally:
            if tags_value is not None:
                cleaned['tags'] = tags_value
