from django.db import models


class JobApplication(models.Model):

    id = models.AutoField(primary_key=True)
    company_name = models.CharField(max_length=512, blank=True, null=True)
    job_title = models.CharField(max_length=512, blank=True, null=True)
    url = models.URLField(blank=True, null=True)
    location = models.CharField(max_length=512, blank=True, null=True)
    is_remote = models.BooleanField(default=False, blank=True, null=True)
    is_agency = models.BooleanField(
        default=False, blank=True, null=True,
        verbose_name="Agency", help_text="Company is hiring on behalf of an external client (staffing/recruitment agency)",
    )
    key_skills = models.TextField(blank=True, null=True)
    soft_skills = models.TextField(blank=True, null=True)
    language = models.CharField(max_length=10, blank=True, null=True)
    german_level = models.CharField(max_length=512, blank=True, null=True)
    required_experience = models.TextField(blank=True, null=True)
    minuses = models.TextField(blank=True, null=True)
    date_added = models.DateTimeField(auto_now_add=True)
    cover_letter = models.TextField(blank=True, null=True)
    cv_intro = models.TextField(blank=True, null=True)
    response_received = models.BooleanField(default=False)
    application_sent = models.BooleanField(default=False, verbose_name="Отклик отправлен")
    not_interested = models.BooleanField(default=False, verbose_name="Неинтересна")
    is_closed = models.BooleanField(default=False, verbose_name="Закрыта")
    phone_interview_date = models.DateTimeField(null=True, blank=True)
    onsite_interview_date = models.DateTimeField(null=True, blank=True)
    contact_person = models.CharField(max_length=512, blank=True, null=True)
    status = models.CharField(
        max_length=50,
        choices=[('pending', 'Pending'), ('accepted', 'Accepted'), ('rejected', 'Rejected')],
        default='pending'
    )
    salary_range = models.CharField(max_length=512, blank=True, null=True)
    vacation_days = models.CharField(max_length=512, blank=True, null=True)
    other_benefits = models.TextField(blank=True, null=True)
    info = models.TextField(blank=True, null=True)
    notes = models.TextField(blank=True, null=True)
    full_text_ru = models.TextField(blank=True, null=True, verbose_name="Full text (RU)")
    company_research = models.TextField(blank=True, null=True, verbose_name="Company research (RU)")
    company_employees = models.CharField(max_length=512, blank=True, null=True, verbose_name="Employees")
    ats_score = models.IntegerField(blank=True, null=True, verbose_name="ATS score (0-10)")
    ats_verdict = models.CharField(max_length=512, blank=True, null=True, verbose_name="ATS verdict")
    ats_assessment = models.TextField(blank=True, null=True, verbose_name="ATS assessment (RU)")
    company_address = models.CharField(max_length=1024, blank=True, null=True, verbose_name="Company address")
    company_lat = models.FloatField(blank=True, null=True, verbose_name="Latitude")
    company_lng = models.FloatField(blank=True, null=True, verbose_name="Longitude")
    company_website = models.CharField(max_length=512, blank=True, null=True, verbose_name="Company website")
    application_instructions = models.TextField(
        blank=True, null=True, verbose_name="Application instructions (RU)",
        help_text="Special instructions on how to apply: cover letter not wanted, specific channel, deadline, etc.",
    )
    is_switzerland = models.BooleanField(
        default=False, verbose_name="Switzerland",
        help_text="Work location is in Switzerland (adds a work-permit note to cv_intro/cover_letter)",
    )


class JobApplicationExperience(models.Model):
    job_application = models.ForeignKey(JobApplication, on_delete=models.CASCADE, related_name='custom_experiences')
    experience = models.ForeignKey('experience.Experience', on_delete=models.CASCADE)
    description = models.TextField(blank=True, null=True)

    class Meta:
        unique_together = ('job_application', 'experience')
