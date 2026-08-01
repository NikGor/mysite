from django.db import migrations


def populate_status(apps, schema_editor):
    JobApplication = apps.get_model('job_application', 'JobApplication')
    for job_application in JobApplication.objects.all():
        if job_application.not_interested:
            new_status = 'not_interested'
        elif job_application.status == 'accepted':
            new_status = 'accepted'
        elif job_application.status == 'rejected':
            new_status = 'rejected'
        elif job_application.is_closed:
            new_status = 'closed'
        elif job_application.response_received:
            has_interview_date = job_application.phone_interview_date or job_application.onsite_interview_date
            new_status = 'interview' if has_interview_date else 'response_received'
        elif job_application.application_sent:
            new_status = 'sent'
        else:
            new_status = 'draft'

        if job_application.status != new_status:
            job_application.status = new_status
            job_application.save(update_fields=['status'])


class Migration(migrations.Migration):

    dependencies = [
        ('job_application', '0039_alter_jobapplication_url'),
    ]

    operations = [
        migrations.RunPython(populate_status, migrations.RunPython.noop),
    ]
