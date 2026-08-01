from rest_framework import serializers
from .models import JobApplication


class JobApplicationSerializer(serializers.ModelSerializer):
    work_mode = serializers.ChoiceField(choices=JobApplication.WORK_MODE_CHOICES, default='office')

    class Meta:
        model = JobApplication
        fields = [
            'company_name', 'job_title', 'url', 'location', 'work_mode',
            'contact_person', 'key_skills', 'soft_skills', 'salary_range',
            'language', 'german_level'
        ]


class ParseTextSerializer(serializers.Serializer):
    text = serializers.CharField()
