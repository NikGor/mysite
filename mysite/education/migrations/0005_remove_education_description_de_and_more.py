# Generated by Django 4.1.13 on 2024-02-26 21:47

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('education', '0004_education_description_de_education_description_en_and_more'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='education',
            name='description_de',
        ),
        migrations.RemoveField(
            model_name='education',
            name='description_en',
        ),
        migrations.RemoveField(
            model_name='education',
            name='description_ru',
        ),
        migrations.RemoveField(
            model_name='education',
            name='faculty_de',
        ),
        migrations.RemoveField(
            model_name='education',
            name='faculty_en',
        ),
        migrations.RemoveField(
            model_name='education',
            name='faculty_ru',
        ),
        migrations.RemoveField(
            model_name='education',
            name='school_de',
        ),
        migrations.RemoveField(
            model_name='education',
            name='school_en',
        ),
        migrations.RemoveField(
            model_name='education',
            name='school_ru',
        ),
    ]