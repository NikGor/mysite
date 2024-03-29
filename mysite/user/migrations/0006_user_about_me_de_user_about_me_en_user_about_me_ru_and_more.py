# Generated by Django 4.2.4 on 2024-02-15 19:29

import ckeditor.fields
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('user', '0005_alter_user_about_me'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='about_me_de',
            field=ckeditor.fields.RichTextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='user',
            name='about_me_en',
            field=ckeditor.fields.RichTextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='user',
            name='about_me_ru',
            field=ckeditor.fields.RichTextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='user',
            name='job_title_de',
            field=models.CharField(blank=True, max_length=100, null=True),
        ),
        migrations.AddField(
            model_name='user',
            name='job_title_en',
            field=models.CharField(blank=True, max_length=100, null=True),
        ),
        migrations.AddField(
            model_name='user',
            name='job_title_ru',
            field=models.CharField(blank=True, max_length=100, null=True),
        ),
    ]
