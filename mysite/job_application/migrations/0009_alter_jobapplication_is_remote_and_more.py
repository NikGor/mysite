# Generated by Django 4.1.13 on 2024-03-03 20:58

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('job_application', '0008_jobapplication_cv_intro'),
    ]

    operations = [
        migrations.AlterField(
            model_name='jobapplication',
            name='is_remote',
            field=models.BooleanField(blank=True, default=False, null=True),
        ),
        migrations.AlterField(
            model_name='jobapplication',
            name='job_title',
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AlterField(
            model_name='jobapplication',
            name='location',
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AlterField(
            model_name='jobapplication',
            name='url',
            field=models.URLField(blank=True, null=True),
        ),
    ]
