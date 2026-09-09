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
