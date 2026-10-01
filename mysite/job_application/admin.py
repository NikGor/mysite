import json
import re

from django import forms
from django.contrib import admin
from django.db.models import Avg, Case, Count, F, FloatField, Q, Sum, Value, When
from django.db.models.functions import Coalesce
from django.urls import reverse
from django.utils.html import escape, format_html, format_html_join
from django.utils.safestring import mark_safe
from django.contrib.auth import get_user_model
from mysite.job_application.models import JobApplication, JobApplicationExperience, LLMCallLog
from mysite.job_application.parser import get_cv_intro, get_cover_letter, \
    get_company_research, get_ats_score, get_company_addresses, geocode_address, extract_company_employees, \
    get_company_website, get_company_reviews, get_client, chat_completion
from scripts.utils import export_cover_letter_txt, export_cover_letter_md, export_cover_letter_pdf, export_cv, \
    export_json


def _tier_score_expression():
    """Mirrors JobApplication.tier_score in SQL so TierFilter can filter on it."""
    return (
        Coalesce(F('ats_score'), Value(0)) * 1.0
        + Case(When(is_agency=True, then=Value(-1.5)), default=Value(1.0), output_field=FloatField())
        + Case(When(has_growth_signal=True, then=Value(1.0)), default=Value(0.0), output_field=FloatField())
        + Case(When(has_toxic_flag=True, then=Value(-3.0)), default=Value(0.0), output_field=FloatField())
        + Case(When(has_overregulated_flag=True, then=Value(-2.0)), default=Value(0.0), output_field=FloatField())
        # German-language daily work is intentionally NOT penalized - not a personal
        # blocker (already working at a German-speaking company).
    )


def _replace_research_section(research_text, header, next_header, new_content):
    """Splices new_content into research_text under `header`, replacing whatever the
    main company_research call put there up to `next_header` (or appending the section
    if `header` isn't present)."""
    pattern = re.escape(header) + r'.*?(?=' + re.escape(next_header) + r')'
    replacement = f"{header}\n{new_content}\n\n"
    new_text, count = re.subn(pattern, replacement, research_text, count=1, flags=re.DOTALL)
    if count:
        return new_text
    return f"{research_text}\n\n{header}\n{new_content}"


class TierFilter(admin.SimpleListFilter):
    title = 'Tier'
    parameter_name = 'tier'

    def lookups(self, request, model_admin):
        return (('S', 'S'), ('A', 'A'), ('B', 'B'), ('C', 'C'), ('D', 'D'), ('N/D', 'N/D (not researched yet)'))

    def queryset(self, request, queryset):
        value = self.value()
        if not value:
            return queryset

        # Tier C is a manual override that wins regardless of score (see JobApplication.tier).
        if value == 'C':
            return queryset.filter(is_high_upside_brand=True)

        queryset = queryset.filter(is_high_upside_brand=False)
        # Unverified agency listings have no meaningful company context (it would
        # describe the agency, not the real employer), so they sit in N/D like an
        # unresearched vacancy - see JobApplication.tier.
        agency_unverified = Q(is_agency=True, agency_end_client_verified=False)
        if value == 'N/D':
            return queryset.filter(Q(ats_score__isnull=True) | agency_unverified)

        queryset = queryset.exclude(agency_unverified).filter(
            ats_score__isnull=False,
        ).annotate(_tier_score=_tier_score_expression())
        if value == 'S':
            return queryset.filter(_tier_score__gte=9)
        if value == 'A':
            return queryset.filter(_tier_score__gte=7, _tier_score__lt=9)
        if value == 'B':
            return queryset.filter(_tier_score__gte=5, _tier_score__lt=7)
        if value == 'D':
            return queryset.filter(_tier_score__lt=5)
        return queryset


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
            return queryset.filter(status='not_interested')
        # Default (no filter selected yet): hide vacancies marked as not interesting
        return queryset.exclude(status='not_interested')


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
                job_application=job_application,
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
                job_application=job_application,
            )

            job_application.save(update_fields=['cv_intro', 'cover_letter'])
        except Exception as e:
            print(f"regenerate_all failed for job_application id={job_application.id}: {e}")

regenerate_all.short_description = "Regenerate all AI content (cv_intro, cover letter)"


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

    agency_skipped = queryset.filter(is_agency=True, agency_end_client_verified=False)
    skipped_count = agency_skipped.count()
    if skipped_count:
        modeladmin.message_user(
            request,
            f"Пропущено {skipped_count} агентских вакансий без подтверждённого работодателя "
            "(company-level research искал бы данные про само агентство, а не про реальное "
            "место работы). Отметьте 'Agency end-client verified good', чтобы включить их.",
        )
    queryset = queryset.exclude(is_agency=True, agency_end_client_verified=False)

    for job_application in queryset:
        location = job_application.location or ''

        if not job_application.company_website:
            job_application.company_website = get_company_website(
                job_application.company_name, location, job_application=job_application,
            )

        # Шаг 1: research — компания должна быть изучена до скоринга, чтобы ATS-оценка
        # могла учитывать размер/тип компании (см. build_company_profile)
        job_application.company_research = get_company_research(
            job_application.company_name, location, job_application=job_application,
        )
        # A single :online search on the main research call rarely surfaces
        # Kununu/Glassdoor pages - dedicated call with a review-site-targeted query.
        reviews_text = get_company_reviews(
            job_application.company_name, location, job_application=job_application,
        )
        job_application.company_research = _replace_research_section(
            job_application.company_research,
            'Отзывы сотрудников (Glassdoor, Kununu, Indeed и т.п.):',
            'Общая репутация и красные флаги:',
            reviews_text,
        )
        job_application.company_employees = extract_company_employees(
            job_application.company_research, job_application=job_application,
        )

        addresses = get_company_addresses(job_application.company_name, location, job_application=job_application)
        offices = []
        for address in addresses:
            geocoded = geocode_address(address)
            if geocoded:
                lat, lng, city_country = geocoded
                offices.append({'address': address, 'lat': lat, 'lng': lng, 'city_country': city_country})

        if not offices:
            job_application.company_address = 'Адрес не найден'
            job_application.company_lat = None
            job_application.company_lng = None
            job_application.company_offices = []
        else:
            primary = offices[0]
            job_application.company_address = primary['address']
            job_application.company_lat = primary['lat']
            job_application.company_lng = primary['lng']
            job_application.company_offices = [
                {'address': o['address'], 'lat': o['lat'], 'lng': o['lng']} for o in offices
            ]
            # Only overwrite the vacancy's location with the researched address for
            # direct employers - for agencies, the researched address is the agency's
            # own HQ, not where the actual end-client job is based.
            if primary['city_country'] and not job_application.is_agency:
                job_application.location = primary['city_country']

        # Шаг 2: скоринг — теперь с учётом профиля компании
        job_summary = build_job_summary(job_application)
        company_profile = build_company_profile(job_application)
        result = get_ats_score(cv_summary, job_summary, company_profile, job_application=job_application)
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
            # Tier signals are required fields on ATSScoreResult - re-derived here with
            # full context (vacancy + company research), overriding the parse-time guess.
            job_application.has_growth_signal = result.get('has_growth_signal', False)
            job_application.has_toxic_flag = result.get('has_toxic_flag', False)
            job_application.has_overregulated_flag = result.get('has_overregulated_flag', False)
            job_application.german_blocks_daily_work = result.get('german_blocks_daily_work', False)

        job_application.save(update_fields=[
            'company_website', 'company_research', 'company_employees',
            'company_address', 'company_lat', 'company_lng', 'company_offices', 'location',
            'ats_score', 'ats_verdict', 'ats_assessment',
            'has_growth_signal', 'has_toxic_flag', 'has_overregulated_flag', 'german_blocks_daily_work',
        ])

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

    class Media:
        css = {'all': ('admin/css/sticky_changelist_header.css',)}
    actions = [
        regenerate_all, research_company,
        export_cover_letter_txt, export_cover_letter_md, export_cover_letter_pdf,
        export_cv, export_json,
    ]
    list_display = (
        'id', 'logo_preview', 'tier_badge', 'ats_badge', 'progress_badge', 'company_link', 'job_title',
        'location_flags', 'employees_short', 'date_added', 'status',
        'phone_interview_date', 'onsite_interview_date',
    )
    list_filter = (
        TierFilter,
        NotInterestedFilter, 'status', 'work_mode', 'is_agency',
        'is_switzerland', 'ats_verdict', 'language', 'german_level', ATSScoreRangeFilter, HasAddressFilter,
        'has_growth_signal', 'has_toxic_flag', 'has_overregulated_flag', 'german_blocks_daily_work',
        'is_high_upside_brand', 'agency_end_client_verified',
    )
    date_hierarchy = 'date_added'
    list_editable = (
        'status', 'phone_interview_date', 'onsite_interview_date',
    )
    search_fields = ('company_name', 'job_title', 'key_skills', 'location', 'contact_person', 'notes')
    readonly_fields = (
        'date_added', 'map_preview', 'url_link', 'llm_cost_display', 'tier_display', 'agency_company_notice',
    )

    def url_link(self, obj):
        if not obj.url:
            return ''
        return format_html('<a href="{0}" target="_blank" rel="noopener noreferrer">{0}</a>', obj.url)

    url_link.short_description = 'Open URL'

    def llm_cost_display(self, obj):
        if not obj.pk:
            return '-'
        calls = obj.llm_calls.all()
        total = calls.aggregate(total=Sum('cost'))['total'] or 0
        if not calls:
            return f"${total:.4f}"
        breakdown = calls.values('purpose').annotate(subtotal=Sum('cost')).order_by('-subtotal')
        rows = format_html_join(
            '', '<tr><td style="padding-right:12px">{}</td><td>${}</td></tr>',
            ((row['purpose'], f"{row['subtotal'] or 0:.4f}") for row in breakdown),
        )
        return format_html('<b>${}</b> total<table>{}</table>', f"{total:.4f}", rows)

    llm_cost_display.short_description = 'LLM cost so far'

    TIER_COLORS = {'S': '#9b59b6', 'A': '#2ecc71', 'B': '#f1c40f', 'C': '#3498db', 'D': '#e74c3c', 'N/D': '#999'}

    def tier_badge(self, obj):
        return format_html(
            '<span style="background:{}; color:#000; padding:2px 8px; border-radius:4px; font-weight:bold">{}</span>',
            self.TIER_COLORS.get(obj.tier, '#999'), obj.tier,
        )

    tier_badge.short_description = 'Tier'

    def ats_badge(self, obj):
        if obj.ats_score is None:
            return format_html('<span style="color:#888">—</span>')
        if obj.ats_score >= 8:
            color = '#2ecc71'
        elif obj.ats_score >= 5:
            color = '#f1c40f'
        else:
            color = '#e74c3c'
        return format_html(
            '<span style="background:{}; color:#000; padding:2px 8px; border-radius:4px; font-weight:bold">{}</span>',
            color, obj.ats_score,
        )

    ats_badge.short_description = 'ATS'
    ats_badge.admin_order_field = 'ats_score'

    PROGRESS_STATES = {
        'draft': ('📝 Черновик', '#999'),
        'sent': ('📨 Отправлено', '#3498db'),
        'response_received': ('💬 Ответ', '#2ecc71'),
        'interview': ('🗓 Собеседование', '#9b59b6'),
        'accepted': ('🎉 Принято', '#27ae60'),
        'rejected': ('❌ Отказ', '#e74c3c'),
        'not_interested': ('🚫 Не интересна', '#555'),
        'closed': ('🔒 Закрыта', '#555'),
    }

    def progress_badge(self, obj):
        label, color = self.PROGRESS_STATES.get(obj.status, ('📝 Черновик', '#999'))
        return format_html(
            '<span style="background:{}; color:#fff; padding:2px 8px; border-radius:4px; '
            'font-size:0.85em; white-space:nowrap">{}</span>',
            color, label,
        )

    progress_badge.short_description = 'Статус'

    def location_flags(self, obj):
        location = obj.location or ''
        # Switzerland has its own model field (drives the work-permit note in
        # cv_intro/cover_letter); Germany doesn't, so it's inferred from location text
        # purely for this display flag.
        is_germany = not obj.is_switzerland and ('germany' in location.lower() or 'deutschland' in location.lower())
        work_mode_icons = {'remote': '🏠', 'hybrid': '🔀'}
        icons = ''.join(filter(None, [
            work_mode_icons.get(obj.work_mode, ''),
            '🏢' if obj.is_agency else '',
            '🇨🇭' if obj.is_switzerland else '',
            '🇩🇪' if is_germany else '',
        ]))
        return format_html('{} {}', location or '—', icons)

    location_flags.short_description = 'Location'
    location_flags.admin_order_field = 'location'

    def employees_short(self, obj):
        if obj.is_agency_unverified:
            return format_html('<span style="color:#888">— агентство —</span>')
        text = obj.company_employees or ''
        if not text:
            return '—'
        short = text if len(text) <= 40 else text[:40].rstrip() + '…'
        return format_html('<span title="{}">{}</span>', text, short)

    employees_short.short_description = 'Employees'

    def tier_display(self, obj):
        if not obj.pk:
            return '-'
        if obj.is_agency_unverified:
            return format_html(
                '<span style="background:{}; color:#000; padding:2px 8px; border-radius:4px; font-weight:bold; '
                'font-size:1.2em">Tier N/D</span> &nbsp; агентство — реальный работодатель не подтверждён',
                self.TIER_COLORS.get('N/D', '#999'),
            )
        return format_html(
            '<span style="background:{}; color:#000; padding:2px 8px; border-radius:4px; font-weight:bold; '
            'font-size:1.2em">Tier {}</span> &nbsp; score: {}{}',
            self.TIER_COLORS.get(obj.tier, '#999'), obj.tier, f"{obj.tier_score:.1f}",
            ' (forced by high-upside brand override)' if obj.is_high_upside_brand else '',
        )

    tier_display.short_description = 'Tier (computed)'

    def agency_company_notice(self, obj):
        return (
            "Агентская вакансия — реальный работодатель неизвестен, поэтому "
            "адрес/сотрудники/ресерч компании не собираются (они относились бы к "
            "самому агентству, а не к месту работы). Отметьте 'Agency end-client "
            "verified good' в разделе Tier scoring, когда узнаете и подтвердите "
            "настоящего работодателя — после этого поля снова станут доступны для "
            "исследования."
        )

    agency_company_notice.short_description = 'Company research (агентство)'

    fieldsets = (
        ('LLM cost', {
            'fields': ('llm_cost_display',)
        }),
        ('Tier scoring', {
            'fields': (
                'tier_display', 'has_growth_signal', 'has_toxic_flag', 'has_overregulated_flag',
                'german_blocks_daily_work', 'is_high_upside_brand', 'agency_end_client_verified',
            )
        }),
        ('ATS score (my CV vs vacancy)', {
            'fields': ('ats_score', 'ats_verdict', 'ats_assessment')
        }),
        ('Vacancy', {
            'fields': (
                'full_text_ru', 'company_name', 'job_title', 'url', 'url_link', 'location', 'work_mode', 'is_agency',
                'is_switzerland', 'contact_person', 'application_instructions',
            )
        }),
        ('Company location (web search)', {
            'fields': ('company_website', 'company_address', 'company_lat', 'company_lng', 'map_preview')
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
                'status', 'phone_interview_date', 'onsite_interview_date', 'date_added',
            )
        }),
    )

    def get_fieldsets(self, request, obj=None):
        fieldsets = super().get_fieldsets(request, obj)
        if not obj or not obj.is_agency_unverified:
            return fieldsets
        # Company-level research would target the agency itself, not the real
        # workplace - replace those fieldsets with a single explanatory notice.
        replaced = []
        for name, opts in fieldsets:
            if name == 'Company location (web search)':
                replaced.append((name, {'fields': ('agency_company_notice',)}))
            elif name == 'Company research (web search)':
                continue
            else:
                replaced.append((name, opts))
        return replaced

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

    def map_preview(self, obj):
        if obj.is_agency_unverified:
            return (
                "Агентская вакансия — реальный работодатель неизвестен, местоположение "
                "не исследуется. Отметьте 'Agency end-client verified good', если "
                "узнали и подтвердили реального работодателя."
            )

        offices = obj.company_offices or []
        if not offices and obj.company_lat is not None and obj.company_lng is not None:
            offices = [{'address': obj.company_address or '', 'lat': obj.company_lat, 'lng': obj.company_lng}]

        if not offices:
            return "Run 'Research company' action to locate this company."

        map_id = f"company-map-{obj.pk}"
        points = [[o['lat'], o['lng']] for o in offices]
        popups = [o.get('address', '') for o in offices]
        center = points[0]

        # json.dumps() output is safe JS but format_html() would HTML-escape its quotes
        # (breaking the JSON) since it doesn't know it's being embedded in a <script>
        # tag - build this one manually with mark_safe instead, escaping the div id
        # (untrusted-ish, derived from pk) and defusing "</script>" in the JSON payload.
        def to_js(value):
            return json.dumps(value).replace('</', '<\\/')

        html = (
            f'<div id="{escape(map_id)}" style="height:400px; max-width:600px; border:1px solid #444"></div>'
            '<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">'
            '<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>'
            '<script>'
            '(function() {'
            f'  var points = {to_js(points)}; var popups = {to_js(popups)};'
            f'  var map = L.map({to_js(map_id)}).setView({to_js(center)}, 13);'
            # OpenStreetMap's own tile.openstreetmap.org server explicitly forbids
            # production/automated use (osm.wiki/Tile_usage_policy) and will 403-ban the
            # source IP - CARTO's free basemaps (no API key, generous usage terms) are
            # the standard drop-in replacement for a low-volume, internal-admin map embed.
            '  L.tileLayer("https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png", {'
            '    subdomains: "abcd", maxZoom: 20,'
            '    attribution: \'&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> '
            'contributors &copy; <a href="https://carto.com/attributions">CARTO</a>\''
            '  }).addTo(map);'
            '  points.forEach(function(p, i) { L.marker(p).addTo(map).bindPopup(popups[i] || ""); });'
            '  if (points.length > 1) { map.fitBounds(L.latLngBounds(points), {padding: [30, 30]}); }'
            '})();'
            '</script>'
        )
        return mark_safe(html)

    map_preview.short_description = 'Map'


class LLMCallStatusFilter(admin.SimpleListFilter):
    title = 'status'
    parameter_name = 'call_status'

    def lookups(self, request, model_admin):
        return (
            ('failed', 'Failed only'),
            ('succeeded', 'Succeeded only'),
        )

    def queryset(self, request, queryset):
        if self.value() == 'failed':
            return queryset.exclude(error__isnull=True).exclude(error='')
        if self.value() == 'succeeded':
            return queryset.filter(Q(error__isnull=True) | Q(error=''))
        return queryset


class LatencyRangeFilter(admin.SimpleListFilter):
    title = 'latency'
    parameter_name = 'latency_range'

    def lookups(self, request, model_admin):
        return (
            ('fast', 'Fast (<3s)'),
            ('medium', 'Medium (3-10s)'),
            ('slow', 'Slow (>10s)'),
        )

    def queryset(self, request, queryset):
        if self.value() == 'fast':
            return queryset.filter(latency_ms__lt=3000)
        if self.value() == 'medium':
            return queryset.filter(latency_ms__gte=3000, latency_ms__lte=10000)
        if self.value() == 'slow':
            return queryset.filter(latency_ms__gt=10000)
        return queryset


def retry_failed_llm_calls(modeladmin, request, queryset):
    client = get_client()
    retried, skipped, failed_again = 0, 0, 0

    for log in queryset:
        if not log.error:
            skipped += 1
            continue

        kwargs = {'model': log.model, 'messages': log.messages}
        if log.temperature is not None:
            kwargs['temperature'] = log.temperature
        if log.max_tokens is not None:
            kwargs['max_tokens'] = log.max_tokens
        if log.response_format is not None:
            kwargs['response_format'] = log.response_format

        try:
            chat_completion(
                client, purpose=log.purpose, job_application=log.job_application, retry_of=log, **kwargs,
            )
            retried += 1
        except Exception as e:
            failed_again += 1
            print(f"Retry failed for LLMCallLog id={log.id}: {e}")

    parts = [f"Retried {retried} call(s)"]
    if failed_again:
        parts.append(f"{failed_again} retry attempt(s) failed again")
    if skipped:
        parts.append(f"{skipped} skipped (no error recorded)")
    modeladmin.message_user(request, ', '.join(parts) + '.')


retry_failed_llm_calls.short_description = "Retry selected calls that failed with an error"


@admin.register(LLMCallLog)
class LLMCallLogAdmin(admin.ModelAdmin):
    list_display = (
        'created_at', 'purpose', 'model', 'latency_display', 'prompt_tokens', 'completion_tokens',
        'cost_display', 'has_error', 'job_application',
    )
    list_filter = (LLMCallStatusFilter, LatencyRangeFilter, 'purpose', 'model')
    actions = [retry_failed_llm_calls]
    search_fields = ('purpose', 'model', 'messages', 'response_text', 'error')
    date_hierarchy = 'created_at'
    ordering = ('-created_at',)
    readonly_fields = (
        'created_at', 'purpose', 'model', 'job_application', 'retry_info', 'latency_display', 'prompt_tokens',
        'completion_tokens', 'total_tokens', 'cost_display', 'messages_display', 'response_text', 'error',
    )
    fields = (
        'created_at', 'purpose', 'model', 'job_application', 'retry_info', 'latency_display', 'prompt_tokens',
        'completion_tokens', 'total_tokens', 'cost_display', 'messages_display', 'response_text', 'error',
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return True

    def has_delete_permission(self, request, obj=None):
        return False

    def cost_display(self, obj):
        return f"${obj.cost:.4f}" if obj.cost is not None else '-'

    cost_display.short_description = 'Cost'

    def latency_display(self, obj):
        return f"{obj.latency_ms} ms" if obj.latency_ms is not None else '-'

    latency_display.short_description = 'Latency'
    latency_display.admin_order_field = 'latency_ms'

    def has_error(self, obj):
        return bool(obj.error)

    has_error.short_description = 'Error'
    has_error.boolean = True

    def messages_display(self, obj):
        if not obj.messages:
            return '-'
        blocks = []
        for message in obj.messages:
            role = message.get('role', '')
            content = message.get('content', '')
            blocks.append(f"### {role}\n{content}")
        text = '\n\n'.join(blocks)
        return format_html('<pre style="white-space: pre-wrap; max-width: 900px">{}</pre>', text)

    messages_display.short_description = 'Prompt (messages sent)'

    def retry_info(self, obj):
        parts = []
        if obj.retry_of_id:
            link = reverse('admin:job_application_llmcalllog_change', args=[obj.retry_of_id])
            parts.append(format_html('Retry of <a href="{}">log #{}</a>', link, obj.retry_of_id))

        retries = obj.retries.all()
        if retries:
            links = format_html_join(
                ', ', '<a href="{}">#{}</a>',
                ((reverse('admin:job_application_llmcalllog_change', args=[r.id]), r.id) for r in retries),
            )
            parts.append(format_html('Retried by: {}', links))

        return format_html('<br>'.join(parts)) if parts else '-'

    retry_info.short_description = 'Retry chain'

    def changelist_view(self, request, extra_context=None):
        response = super().changelist_view(request, extra_context=extra_context)
        try:
            queryset = response.context_data['cl'].queryset
        except (AttributeError, KeyError):
            return response

        total_cost = queryset.aggregate(total=Sum('cost'))['total'] or 0
        response.context_data['total_cost_display'] = f"${total_cost:.4f}"

        purpose_stats = list(
            queryset.values('purpose')
            .annotate(count=Count('id'), avg_latency=Avg('latency_ms'), avg_cost=Avg('cost'), total_cost=Sum('cost'))
            .order_by('-total_cost')
        )
        for stat in purpose_stats:
            stat['avg_latency'] = int(stat['avg_latency']) if stat['avg_latency'] is not None else None
        response.context_data['purpose_stats'] = purpose_stats

        return response

    change_list_template = 'admin/job_application/llmcalllog/change_list.html'
