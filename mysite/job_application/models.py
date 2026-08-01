from django.db import models


class JobApplication(models.Model):

    id = models.AutoField(primary_key=True)
    company_name = models.CharField(max_length=512, blank=True, null=True)
    job_title = models.CharField(max_length=512, blank=True, null=True)
    url = models.URLField(max_length=2048, blank=True, null=True)
    location = models.CharField(max_length=512, blank=True, null=True)
    WORK_MODE_CHOICES = [('remote', 'Remote'), ('hybrid', 'Hybrid'), ('office', 'Office')]
    work_mode = models.CharField(
        max_length=10, choices=WORK_MODE_CHOICES, default='office', blank=True, null=True,
        verbose_name="Work mode",
    )
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
    phone_interview_date = models.DateTimeField(null=True, blank=True)
    onsite_interview_date = models.DateTimeField(null=True, blank=True)
    contact_person = models.CharField(max_length=512, blank=True, null=True)
    STATUS_CHOICES = [
        ('draft', 'Черновик'),
        ('sent', 'Отправлено'),
        ('response_received', 'Ответ получен'),
        ('interview', 'Собеседование'),
        ('accepted', 'Принято'),
        ('rejected', 'Отказ'),
        ('not_interested', 'Неинтересна'),
        ('closed', 'Закрыта'),
    ]
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='draft')
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
    company_offices = models.JSONField(
        default=list, blank=True, verbose_name="Company offices",
        help_text="All known offices found during research: [{address, lat, lng}, ...]. First entry mirrors "
                   "company_address/company_lat/company_lng.",
    )
    company_website = models.CharField(max_length=512, blank=True, null=True, verbose_name="Company website")
    application_instructions = models.TextField(
        blank=True, null=True, verbose_name="Application instructions (RU)",
        help_text="Special instructions on how to apply: cover letter not wanted, specific channel, deadline, etc.",
    )
    is_switzerland = models.BooleanField(
        default=False, verbose_name="Switzerland",
        help_text="Work location is in Switzerland (adds a work-permit note to cv_intro/cover_letter)",
    )

    # Tier-scoring signals — inferred by the LLM from the vacancy text at parse time.
    has_growth_signal = models.BooleanField(
        default=False, verbose_name="Growth signal",
        help_text="Vacancy signals company growth/stability (funding round, growing team, named clients, YoY growth)",
    )
    has_toxic_flag = models.BooleanField(
        default=False, verbose_name="Toxic culture flag",
        help_text="Vacancy signals a toxic/high-pressure culture (\"top-1%\", \"extreme ownership\", long hours)",
    )
    has_overregulated_flag = models.BooleanField(
        default=False, verbose_name="Overregulated flag",
        help_text="Vacancy signals a heavily regulated environment that limits day-to-day experimentation",
    )
    german_blocks_daily_work = models.BooleanField(
        default=False, verbose_name="German blocks daily work",
        help_text="German is required for actual day-to-day work, not just a nice-to-have",
    )
    # Manual-only override — never set by the LLM. See tier property: forces Tier C for
    # exceptional-brand/high-upside bets that wouldn't otherwise qualify (e.g. Google).
    is_high_upside_brand = models.BooleanField(
        default=False, verbose_name="High-upside brand (manual override)",
        help_text="Manually flag an asymmetric-upside bet (brand/scale) that should sit in Tier C regardless of score",
    )
    # Manual-only override — agency listings are capped at Tier B by default (the real
    # employer is unknown), even if their raw score would reach A/S. Check this box once
    # you've learned who the actual end client is and confirmed it's a good one, to lift
    # the cap and let the vacancy score normally.
    agency_end_client_verified = models.BooleanField(
        default=False, verbose_name="Agency end-client verified good",
        help_text="Real employer behind this agency listing is known and good - lifts the default Tier B cap",
    )

    @property
    def is_agency_unverified(self):
        """True while the vacancy is an agency listing whose real end-client employer is
        still unknown - company-level research (address/employees/reviews) would target
        the agency itself, not the actual workplace, so it isn't useful here."""
        return self.is_agency and not self.agency_end_client_verified

    @property
    def tier_score(self):
        score = float(self.ats_score or 0)
        score += -1.5 if self.is_agency else 1.0
        score += 1.0 if self.has_growth_signal else 0.0
        score += -3.0 if self.has_toxic_flag else 0.0
        score += -2.0 if self.has_overregulated_flag else 0.0
        # German-language daily work is intentionally NOT penalized here - not a
        # personal blocker (already working at a German-speaking company). The flag is
        # still tracked/shown for reference, it just doesn't move the tier score.
        return score

    @property
    def tier(self):
        if self.is_high_upside_brand:
            return 'C'
        # Company-level context (size, reputation, red flags) feeds into the tier score,
        # but for an unverified agency listing that context describes the agency, not
        # the real employer - the tier would be meaningless until the end client is
        # known, so it stays N/D like an unresearched vacancy.
        if self.ats_score is None or self.is_agency_unverified:
            return 'N/D'
        score = self.tier_score
        if score >= 9:
            return 'S'
        if score >= 7:
            return 'A'
        if score >= 5:
            return 'B'
        return 'D'


class JobApplicationExperience(models.Model):
    job_application = models.ForeignKey(JobApplication, on_delete=models.CASCADE, related_name='custom_experiences')
    experience = models.ForeignKey('experience.Experience', on_delete=models.CASCADE)
    description = models.TextField(blank=True, null=True)

    class Meta:
        unique_together = ('job_application', 'experience')


class LLMCallLog(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    purpose = models.CharField(max_length=255, help_text="Which function/feature made this call, e.g. 'cover_letter'")
    model = models.CharField(max_length=255)
    messages = models.JSONField(null=True, blank=True, help_text="Full list of messages sent to the LLM (system/user/etc.)")
    response_text = models.TextField(null=True, blank=True, help_text="Raw text of the LLM response")
    latency_ms = models.IntegerField(null=True, blank=True, help_text="Wall-clock time for the API call, in milliseconds")
    prompt_tokens = models.IntegerField(null=True, blank=True)
    completion_tokens = models.IntegerField(null=True, blank=True)
    total_tokens = models.IntegerField(null=True, blank=True)
    cost = models.DecimalField(max_digits=10, decimal_places=6, null=True, blank=True, help_text="USD, as reported by OpenRouter")
    error = models.TextField(null=True, blank=True, help_text="Exception message if the call failed")
    temperature = models.FloatField(null=True, blank=True)
    max_tokens = models.IntegerField(null=True, blank=True)
    response_format = models.JSONField(null=True, blank=True, help_text="response_format kwarg sent to the LLM, if any")
    job_application = models.ForeignKey(
        JobApplication, on_delete=models.SET_NULL, null=True, blank=True, related_name='llm_calls',
    )
    retry_of = models.ForeignKey(
        'self', on_delete=models.SET_NULL, null=True, blank=True, related_name='retries',
        help_text="If this call is a manual retry of a failed call, the original log entry",
    )

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.purpose} ({self.model}) - ${self.cost}"
