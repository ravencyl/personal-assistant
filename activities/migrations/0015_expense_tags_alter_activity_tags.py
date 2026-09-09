# 费用标签新增 + 活动标签从 taggit 换到 core.Tag M2M。
# RemoveField + AddField 组合（不能用 AlterField：Django 禁止 M2M 类型互换，
# 实测 ValueError）。RemoveField 对 taggit 字段是安全的——TaggableManager 的
# through 是显式定义的 taggit.TaggedItem（非 auto_created），schema editor
# 不会 drop 共享的 taggit_taggeditem 表；数据由 0016 data migration 搬走。
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('activities', '0014_expensecategory_alter_expense_category'),
        ('core', '0008_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='expense',
            name='tags',
            field=models.ManyToManyField(blank=True, to='core.tag', verbose_name='标签'),
        ),
        migrations.RemoveField(
            model_name='activity',
            name='tags',
        ),
        migrations.AddField(
            model_name='activity',
            name='tags',
            field=models.ManyToManyField(blank=True, to='core.tag', verbose_name='标签'),
        ),
    ]
