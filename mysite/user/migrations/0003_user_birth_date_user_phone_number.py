# Generated by Django 4.1.7 on 2023-08-26 22:20

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("user", "0002_user_linkedin"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="birth_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="user",
            name="phone_number",
            field=models.CharField(blank=True, max_length=15, null=True),
        ),
    ]
