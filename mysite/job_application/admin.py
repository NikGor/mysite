from django import forms
from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html
from django.contrib.auth import get_user_model
from mysite.job_application.models import JobApplication, JobApplicationExperience
from mysite.job_application.parser import get_experience_description, get_cv_intro, get_cover_letter, \
    get_company_research, get_ats_score, get_company_address, geocode_address, extract_company_employees, \
    get_company_website
from scripts.utils import export_cover_letter, export_cv, export_json


class ATSScoreRangeFilter(admin.SimpleListFilter):
    title = 'ATS score range'
    parameter_name = 'ats_score_range'

    def lookups(self, request, model_admin):
        return (
            ('high', 'High (8-10)'),
            ('medium', 'Medium (5-7)'),
            ('low', 'Low (0-4)'),
            ('none', 'Not scored yet'),
        )

    def queryset(self, request, queryset):
        if self.value() == 'high':
            return queryset.filter(ats_score__gte=8)
        if self.value() == 'medium':
            return queryset.filter(ats_score__gte=5, ats_score__lte=7)
        if self.value() == 'low':
            return queryset.filter(ats_score__lte=4)
        if self.value() == 'none':
            return queryset.filter(ats_score__isnull=True)
        return queryset


class HasAddressFilter(admin.SimpleListFilter):
    title = 'company location'
    parameter_name = 'has_address'

    def lookups(self, request, model_admin):
        return (
            ('yes', 'Located on map'),
            ('no', 'Not located yet'),
        )

    def queryset(self, request, queryset):
        if self.value() == 'yes':
            return queryset.filter(company_lat__isnull=False)
        if self.value() == 'no':
            return queryset.filter(company_lat__isnull=True)
        return queryset


class NotInterestedFilter(admin.SimpleListFilter):
    title = 'not interested'
    parameter_name = 'not_interested'

    def lookups(self, request, model_admin):
        return (
            ('show_all', 'Show all (incl. not interesting)'),
            ('only', 'Only not interesting'),
        )

    def queryset(self, request, queryset):
        if self.value() == 'show_all':
            return queryset
        if self.value() == 'only':
            return queryset.filter(not_interested=True)
        # Default (no filter selected yet): hide vacancies marked as not interesting
        return queryset.filter(not_interested=False)


def regenerate_all(modeladmin, request, queryset):
    for job_application in queryset:
        try:
            user = get_user_model().objects.first()
            language = job_application.language or 'en'
            key_skills = job_application.key_skills or ''
            soft_skills = job_application.soft_skills or ''

            cv_intro_sample = getattr(user, f'about_me_{language}', user.about_me)
            job_application.cv_intro = get_cv_intro(
                None, cv_intro_sample, language, key_skills, soft_skills,
                is_switzerland=job_application.is_switzerland,
            )

            job_application.cover_letter = get_cover_letter(
                None,
                job_application.job_title,
                job_application.company_name,
                user.cover_letter_sample or '',
                language,
                key_skills,
                soft_skills,
                job_application.contact_person or '',
                sender_name=f"{user.first_name} {user.last_name}",
                cv_intro=job_application.cv_intro or '',
                ats_context=job_application.ats_assessment or '',
                company_context=job_application.company_research or '',
                is_agency=job_application.is_agency or False,
                is_switzerland=job_application.is_switzerland,
            )

            experiences = user.experience_set.all().order_by('order')
            for experience in experiences:
                original = getattr(experience, f'description_{language}', None) or experience.description
                description = get_experience_description(
                    original_description=original,
                    job_title=job_application.job_title,
                    company_name=job_application.company_name,
                    language=language,
                    key_skills=key_skills,
                    soft_skills=soft_skills,
                )
                JobApplicationExperience.objects.update_or_create(
                    job_application=job_application,
                    experience=experience,
                    defaults={'description': description},
                )

            job_application.save()
        except Exception as e:
            print(f"regenerate_all failed for job_application id={job_application.id}: {e}")

regenerate_all.short_description = "Regenerate all AI content (cv_intro, cover letter, experience)"


def build_cv_summary(user):
    parts = [f"О себе: {user.about_me or ''}"]

    skills = user.skill_set.all().order_by('order')
    if skills:
        skills_text = '\n'.join(f"- {skill.skill_type}: {skill.description}" for skill in skills)
        parts.append(f"Навыки:\n{skills_text}")

    experiences = user.experience_set.all().order_by('order')
    if experiences:
        experience_text = '\n'.join(
            f"- {exp.job_title} @ {exp.company} ({exp.start_date} - {exp.end_date or 'настоящее время'}): {exp.description}"
            for exp in experiences
        )
        parts.append(f"Опыт работы:\n{experience_text}")

    educations = user.education_set.all().order_by('order')
    if educations:
        education_text = '\n'.join(
            f"- {edu.school}, {edu.faculty} ({edu.start_date} - {edu.end_date or ''})"
            for edu in educations
        )
        parts.append(f"Образование:\n{education_text}")

    return '\n\n'.join(parts)


def build_job_summary(job_application):
    return (
        f"Должность: {job_application.job_title or ''}\n"
        f"Компания: {job_application.company_name or ''}\n"
        f"Требуемый опыт: {job_application.required_experience or ''}\n"
        f"Ключевые навыки: {job_application.key_skills or ''}\n"
        f"Софт-скиллы: {job_application.soft_skills or ''}\n"
        f"Уровень немецкого: {job_application.german_level or ''}\n\n"
        f"Полное описание вакансии:\n{job_application.full_text_ru or ''}"
    )


def build_company_profile(job_application):
    return (
        f"Компания: {job_application.company_name or ''}\n"
        f"Число сотрудников: {job_application.company_employees or 'неизвестно'}\n"
        f"Агентство (нанимает для внешнего клиента): {'Да' if job_application.is_agency else 'Нет'}\n\n"
        f"{job_application.company_research or ''}"
    )


def research_company(modeladmin, request, queryset):
    user = get_user_model().objects.first()
    cv_summary = build_cv_summary(user)

    for job_application in queryset:
        location = job_application.location or ''

        if not job_application.company_website:
            job_application.company_website = get_company_website(job_application.company_name, location)

        # Шаг 1: research — компания должна быть изучена до скоринга, чтобы ATS-оценка
        # могла учитывать размер/тип компании (см. build_company_profile)
        job_application.company_research = get_company_research(job_application.company_name, location)
        job_application.company_employees = extract_company_employees(job_application.company_research)

        address = get_company_address(job_application.company_name, location)
        if not address or address.strip() == 'Адрес не найден':
            job_application.company_address = address or ''
            job_application.company_lat = None
            job_application.company_lng = None
        else:
            job_application.company_address = address
            coords = geocode_address(address)
            if coords:
                job_application.company_lat, job_application.company_lng = coords

        # Шаг 2: скоринг — теперь с учётом профиля компании
        job_summary = build_job_summary(job_application)
        company_profile = build_company_profile(job_application)
        result = get_ats_score(cv_summary, job_summary, company_profile)
        if 'error' not in result:
            score = result.get('score')
            verdict = result.get('verdict', '')
            matched = '\n'.join(f"- {skill}" for skill in result.get('matched_skills', [])) or 'Нет'
            missing = '\n'.join(f"- {skill}" for skill in result.get('missing_skills', [])) or 'Нет'
            hard_filters = '\n'.join(f"- {risk}" for risk in result.get('hard_filter_risks', [])) or 'Не выявлено'

            job_application.ats_score = score
            job_application.ats_verdict = verdict
            job_application.ats_assessment = (
                f"Оценка: {score}/10 — {verdict}\n\n"
                f"{result.get('summary', '')}\n\n"
                f"Риски отсева по hard-фильтрам (до скоринга):\n{hard_filters}\n\n"
                f"Контекст компании:\n{result.get('company_fit_note', '')}\n\n"
                f"Совпадающие навыки:\n{matched}\n\n"
                f"Не хватает:\n{missing}"
            )

        job_application.save()

research_company.short_description = "Research company (web search + address/map + ATS score, Gemini 2.5)"


class JobApplicationExperienceInline(admin.TabularInline):
    model = JobApplicationExperience
    extra = 1
    verbose_name_plural = "Custom Experience Descriptions"
    fields = ('experience', 'description')


class JobApplicationForm(forms.ModelForm):
    class Meta:
        model = JobApplication
        fields = '__all__'
        widgets = {
            'location': forms.TextInput(attrs={'size': 60}),
            'contact_person': forms.TextInput(attrs={'size': 40}),
            'salary_range': forms.TextInput(attrs={'size': 40}),
            'vacation_days': forms.TextInput(attrs={'size': 20}),
            'required_experience': forms.TextInput(attrs={'size': 90}),
            'key_skills': forms.Textarea(attrs={'rows': 3, 'cols': 90}),
            'soft_skills': forms.Textarea(attrs={'rows': 3, 'cols': 90}),
            'other_benefits': forms.Textarea(attrs={'rows': 4, 'cols': 90}),
            'minuses': forms.Textarea(attrs={'rows': 4, 'cols': 90}),
            'info': forms.Textarea(attrs={'rows': 4, 'cols': 90}),
            'notes': forms.Textarea(attrs={'rows': 3, 'cols': 90}),
            'cover_letter': forms.Textarea(attrs={'rows': 10, 'cols': 90}),
            'cv_intro': forms.Textarea(attrs={'rows': 10, 'cols': 90}),
            'full_text_ru': forms.Textarea(attrs={'rows': 22, 'cols': 90}),
            'company_research': forms.Textarea(attrs={'rows': 12, 'cols': 90}),
            'ats_assessment': forms.Textarea(attrs={'rows': 10, 'cols': 90}),
        }


@admin.register(JobApplication)
class JobApplicationAdmin(admin.ModelAdmin):
    form = JobApplicationForm
    inlines = [JobApplicationExperienceInline]
    actions = [regenerate_all, research_company, export_cover_letter, export_cv, export_json]
    list_display = (
        'id', 'logo_preview', 'date_added', 'company_link', 'job_title', 'location',
        'is_remote', 'is_agency', 'is_switzerland', 'company_employees',
        'ats_score', 'ats_verdict', 'required_experience', 'salary_range', 'application_instructions_icon',
        'application_sent', 'response_received',
        'not_interested', 'is_closed', 'status', 'phone_interview_date', 'onsite_interview_date', 'contact_person',
    )
    list_filter = (
        NotInterestedFilter, 'status', 'application_sent', 'response_received', 'is_closed', 'is_remote', 'is_agency',
        'is_switzerland', 'ats_verdict', 'language', 'german_level', ATSScoreRangeFilter, HasAddressFilter,
    )
    date_hierarchy = 'date_added'
    list_editable = (
        'application_sent', 'response_received', 'not_interested', 'is_closed', 'phone_interview_date',
        'onsite_interview_date', 'status',
    )
    search_fields = ('company_name', 'job_title', 'key_skills', 'location', 'contact_person', 'notes')
    readonly_fields = ('date_added', 'map_preview')

    fieldsets = (
        ('ATS score (my CV vs vacancy)', {
            'fields': ('ats_score', 'ats_verdict', 'ats_assessment')
        }),
        ('Vacancy', {
            'fields': (
                'full_text_ru', 'company_name', 'job_title', 'url', 'location', 'is_remote', 'is_agency',
                'is_switzerland', 'contact_person', 'company_website', 'company_address', 'map_preview',
                'application_instructions',
            )
        }),
        ('Language & requirements', {
            'fields': ('language', 'german_level', 'required_experience', 'salary_range', 'vacation_days')
        }),
        ('Skills', {
            'fields': ('key_skills', 'soft_skills')
        }),
        ('Assessment', {
            'fields': ('other_benefits', 'minuses', 'info', 'notes')
        }),
        ('Company research (web search)', {
            'fields': ('company_employees', 'company_research')
        }),
        ('Generated content', {
            'fields': ('cover_letter', 'cv_intro')
        }),
        ('Application status', {
            'fields': (
                'application_sent', 'response_received', 'not_interested', 'is_closed', 'phone_interview_date',
                'onsite_interview_date', 'status', 'date_added',
            )
        }),
    )

    def company_link(self, obj):
        link = reverse("admin:job_application_jobapplication_change", args=[obj.pk])
        return format_html('<a href="{}">{}</a>', link, obj.company_name)

    company_link.short_description = 'company_name'

    def logo_preview(self, obj):
        if not obj.company_website:
            return ""
        icon_url = (
            "https://t1.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON"
            f"&fallback_opts=TYPE,SIZE,URL&url=https://{obj.company_website}&size=32"
        )
        return format_html(
            '<a href="https://{0}" target="_blank" rel="noopener noreferrer">'
            '<img src="{1}" width="20" height="20" style="vertical-align:middle"></a>',
            obj.company_website, icon_url,
        )

    logo_preview.short_description = ''

    def application_instructions_icon(self, obj):
        if not obj.application_instructions:
            return ""
        return format_html(
            '<div style="max-width:300px; white-space:normal">{0}</div>',
            obj.application_instructions,
        )

    application_instructions_icon.short_description = 'Application instructions'

    def map_preview(self, obj):
        if obj.company_lat is None or obj.company_lng is None:
            return "Run 'Research company' action to locate this company."

        delta = 0.005
        bbox = (
            f"{obj.company_lng - delta}%2C{obj.company_lat - delta}%2C"
            f"{obj.company_lng + delta}%2C{obj.company_lat + delta}"
        )
        embed_url = (
            f"https://www.openstreetmap.org/export/embed.html?bbox={bbox}"
            f"&layer=mapnik&marker={obj.company_lat}%2C{obj.company_lng}"
        )
        full_url = f"https://www.openstreetmap.org/?mlat={obj.company_lat}&mlon={obj.company_lng}#map=16"

        return format_html(
            '<iframe width="400" height="400" style="border:1px solid #444; max-width:100%" '
            'src="{}" loading="lazy"></iframe><br><a href="{}" target="_blank">Open larger map</a>',
            embed_url, full_url,
        )

    map_preview.short_description = 'Map'
