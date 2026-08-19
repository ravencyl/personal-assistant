from taggit.forms import TagField


class PlainTagField(TagField):
    """标签字段：把 Tag 对象列表格式化为逗号分隔文本，配合普通 TextInput 渲染。

    taggit 默认的 formfield 与 TextInput 组合时，编辑页会把值渲染为
    `[<Tag: ...>]` 对象列表，此字段覆写 prepare_value 解决回显问题。
    """

    def prepare_value(self, value):
        if value is None:
            return ''
        if isinstance(value, str):
            return value
        if hasattr(value, 'names'):
            return ', '.join(value.names())
        return ', '.join(str(tag) for tag in value)
