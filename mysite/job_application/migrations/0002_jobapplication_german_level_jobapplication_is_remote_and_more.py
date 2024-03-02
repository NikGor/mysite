# Generated by Django 4.1.13 on 2024-03-02 10:37

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('job_application', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='jobapplication',
            name='german_level',
            field=models.CharField(blank=True, max_length=20, null=True),
        ),
        migrations.AddField(
            model_name='jobapplication',
            name='is_remote',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='jobapplication',
            name='key_skills',
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='jobapplication',
            name='language',
            field=models.CharField(blank=True, max_length=10, null=True),
        ),
        migrations.AddField(
            model_name='jobapplication',
            name='soft_skills',
            field=models.TextField(blank=True, null=True),
        ),
    ]