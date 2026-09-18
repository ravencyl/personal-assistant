from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('knowledge', '0005_alter_article_tags'),
    ]

    operations = [
        migrations.CreateModel(
            name='ArticleVersion',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('content', models.TextField(verbose_name='内容快照')),
                ('title', models.CharField(max_length=255, verbose_name='标题快照')),
                ('edited_at', models.DateTimeField(auto_now_add=True)),
                ('edit_note', models.CharField(blank=True, max_length=200, verbose_name='编辑备注')),
                ('article', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='versions', to='knowledge.article')),
            ],
            options={
                'verbose_name': '文章版本',
                'verbose_name_plural': '文章版本',
                'ordering': ['-edited_at'],
            },
        ),
    ]
