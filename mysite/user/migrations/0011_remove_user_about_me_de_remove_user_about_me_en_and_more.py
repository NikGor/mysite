# Generated by Django 5.0.1 on 2024-02-27 00:55

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('user', '0010_user_about_me_de_user_about_me_en_user_job_title_de_and_more'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='user',
            name='about_me_de',
        ),
        migrations.RemoveField(
            model_name='user',
            name='about_me_en',
        ),
        migrations.RemoveField(
            model_name='user',
            name='job_title_de',
        ),
        migrations.RemoveField(
            model_name='user',
            name='job_title_en',
        ),
    ]
