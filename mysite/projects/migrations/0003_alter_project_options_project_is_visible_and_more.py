# Generated by Django 4.1.7 on 2023-08-27 18:12

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("projects", "0002_project_github_url_alter_project_url"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="project",
            options={"ordering": ["order"]},
        ),
        migrations.AddField(
            model_name="project",
            name="is_visible",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="project",
            name="order",
            field=models.IntegerField(default=0),
        ),
    ]
