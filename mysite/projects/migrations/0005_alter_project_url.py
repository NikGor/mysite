# Generated by Django 4.1.7 on 2023-08-29 18:45

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("projects", "0004_alter_project_screenshot_url"),
    ]

    operations = [
        migrations.AlterField(
            model_name="project",
            name="url",
            field=models.CharField(blank=True, max_length=200, null=True),
        ),
    ]
