from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('job_application', '0040_populate_unified_status'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='jobapplication',
            name='application_sent',
        ),
        migrations.RemoveField(
            model_name='jobapplication',
            name='response_received',
        ),
        migrations.RemoveField(
            model_name='jobapplication',
            name='not_interested',
        ),
        migrations.RemoveField(
            model_name='jobapplication',
            name='is_closed',
        ),
        migrations.AlterField(
            model_name='jobapplication',
            name='status',
            field=models.CharField(
                choices=[
                    ('draft', 'Черновик'),
                    ('sent', 'Отправлено'),
                    ('response_received', 'Ответ получен'),
                    ('interview', 'Собеседование'),
                    ('accepted', 'Принято'),
                    ('rejected', 'Отказ'),
                    ('not_interested', 'Неинтересна'),
                    ('closed', 'Закрыта'),
                ],
                default='draft',
                max_length=50,
            ),
        ),
    ]
