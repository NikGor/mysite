# Generated by Django 4.1.13 on 2024-03-03 14:20

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('job_application', '0003_remove_jobapplication_final_decision_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='jobapplication',
            name='other_benefits',
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name='jobapplication',
            name='vacation_days',
            field=models.CharField(blank=True, max_length=10, null=True),
        ),
    ]