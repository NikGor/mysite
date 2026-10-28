"""Job-application pipeline steps shared by the API views, the admin actions and the
Gmail import command. Fixed DAG, no agent loop - every step is known up front:

    ingest:   [relevance gate] -> extraction || RU translation -> save JobApplication
    research: (research || reviews || facts) -> employees -> geocode -> ATS score -> save
"""
import contextvars
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from django.db import close_old_connections
from django.db.models import Sum
from django.utils import timezone

from mysite.job_application.models import JobApplication, LLMCallLog
from mysite.job_application.parser import (
    VacancyExtraction, attach_llm_calls, check_vacancy_relevance, extract_company_employees, extract_vacancy,
    geocode_address, get_ats_score, get_company_facts, get_company_research, get_company_reviews, get_full_text_ru,
    track_llm_calls,
)

# Company-level research (profile, reviews, size) barely changes within weeks, and
# LinkedIn alerts keep re-surfacing the same employers - reuse it across vacancies.
RESEARCH_CACHE_DAYS = 30

REVIEWS_HEADER = 'Отзывы сотрудников (Glassdoor, Kununu, Indeed и т.п.):'
REPUTATION_HEADER = 'Общая репутация и красные флаги:'

RESEARCH_FIELDS = [
    'company_website', 'company_research', 'company_employees',
    'company_address', 'company_lat', 'company_lng', 'company_offices', 'location',
    'ats_score', 'ats_verdict', 'ats_assessment',
    'has_growth_signal', 'has_toxic_flag', 'has_overregulated_flag', 'german_blocks_daily_work',
]


def _in_thread(ctx, fn, args, kwargs):
    try:
        return ctx.run(fn, *args, **kwargs)
    finally:
        # Each worker thread opens its own DB connection (LLMCallLog writes) - close it.
        close_old_connections()


def run_parallel(*calls):
    """Runs independent (fn, args, kwargs) steps concurrently and returns their results
    in order. The caller's context is copied into each thread, so LLM calls made there
    are still collected by an enclosing track_llm_calls()."""
    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        futures = [
            pool.submit(_in_thread, contextvars.copy_context(), fn, args, kwargs)
            for fn, args, kwargs in calls
        ]
        return [future.result() for future in futures]


def llm_cost_since(started_at):
    return LLMCallLog.objects.filter(created_at__gte=started_at).aggregate(total=Sum('cost'))['total'] or 0


@dataclass
class IngestResult:
    job_application: Optional[JobApplication] = None
    skip_reason: str = ''
    relevance: Optional[float] = None


class VacancyIngestError(Exception):
    pass


def _bullets(items):
    return '\n'.join(f"- {item}" for item in items)


def build_job_application(extraction: VacancyExtraction, **overrides):
    fields = dict(
        company_name=extraction.company_name,
        job_title=extraction.job_title,
        location=', '.join(extraction.location),
        work_mode=extraction.work_mode,
        is_agency=extraction.is_agency,
        contact_person=extraction.contact_person,
        key_skills=', '.join(extraction.skills),
        soft_skills=', '.join(extraction.soft_skills),
        required_experience=extraction.required_experience,
        salary_range=extraction.salary_range,
        vacation_days=extraction.vacation_days,
        other_benefits=_bullets(extraction.other_benefits),
        minuses=_bullets(extraction.minuses),
        language=extraction.language,
        german_level=extraction.german,
        info=extraction.info,
        application_instructions=extraction.application_instructions,
        is_switzerland=extraction.is_switzerland,
        has_growth_signal=extraction.has_growth_signal,
        has_toxic_flag=extraction.has_toxic_flag,
        has_overregulated_flag=extraction.has_overregulated_flag,
        german_blocks_daily_work=extraction.german_blocks_daily_work,
    )
    fields.update(overrides)
    return JobApplication(**fields)


def ingest_vacancy(text, url=None, date_added=None, status='draft', info=None,
                   cv_summary=None, relevance_threshold=None):
    """Extracts a vacancy and saves it as a JobApplication. cv_intro/cover_letter are
    deliberately not generated here - only extraction, so no LLM spend happens before the
    manual decision to apply ("Regenerate all AI content" admin action does that).

    With cv_summary + relevance_threshold, clearly off-field vacancies are dropped
    before any extraction spend. Raises VacancyIngestError if extraction is unusable."""
    from mysite.job_application.parser import LLMOutputError

    with track_llm_calls() as call_ids:
        relevance = None
        if cv_summary is not None and relevance_threshold is not None:
            relevance = check_vacancy_relevance(cv_summary, text)
            if relevance is not None and relevance < relevance_threshold:
                return IngestResult(skip_reason=f'not relevant (jev={relevance:.2f})', relevance=relevance)

        def safe_extract():
            try:
                return extract_vacancy(text)
            except LLMOutputError as e:
                return e

        extraction, full_text_ru = run_parallel(
            (safe_extract, (), {}),
            (get_full_text_ru, (text,), {}),
        )
        if isinstance(extraction, Exception):
            raise VacancyIngestError(f'extraction error: {extraction}') from extraction

        overrides = dict(url=url, full_text_ru=full_text_ru, status=status)
        if info is not None:
            overrides['info'] = info
        job_application = build_job_application(extraction, **overrides)
        job_application.save()
        if date_added is not None:
            # date_added is auto_now_add - only an update() can set it to the email's date.
            JobApplication.objects.filter(pk=job_application.pk).update(date_added=date_added)
            job_application.date_added = date_added

    attach_llm_calls(call_ids, job_application)
    return IngestResult(job_application=job_application, relevance=relevance)


def _replace_research_section(research_text, header, next_header, new_content):
    """Swaps the body of one section in the research report (between `header` and
    `next_header`) for `new_content`. If the header isn't found, appends a new section."""
    pattern = re.compile(re.escape(header) + r'.*?(?=' + re.escape(next_header) + r')', re.DOTALL)
    replacement = f"{header}\n{new_content}\n\n"
    if pattern.search(research_text):
        return pattern.sub(lambda _: replacement, research_text, count=1)
    return f"{research_text.rstrip()}\n\n{header}\n{new_content}"


def _city(location):
    """'Zürich, Switzerland' and 'Zürich' are the same place for office ordering -
    research rewrites a direct employer's location to the geocoder's 'City, Country'."""
    city = unicodedata.normalize('NFKD', (location or '').split(',')[0].lower())
    return ' '.join(''.join(c for c in city if not unicodedata.combining(c)).split())


def find_cached_research(job_application):
    """Most recent other vacancy of the same company whose company research succeeded
    within RESEARCH_CACHE_DAYS, or None."""
    if not job_application.company_name:
        return None
    since = timezone.now() - timedelta(days=RESEARCH_CACHE_DAYS)
    return (
        JobApplication.objects
        .filter(company_name__iexact=job_application.company_name.strip())
        .exclude(pk=job_application.pk)
        .exclude(company_research__isnull=True).exclude(company_research='')
        .filter(llm_calls__purpose='company_research', llm_calls__error__isnull=True,
                llm_calls__created_at__gte=since)
        .order_by('-llm_calls__created_at')
        .first()
    )


def _geocode_offices(addresses):
    offices = []
    for address in addresses:
        geocoded = geocode_address(address)
        if geocoded:
            lat, lng, city_country = geocoded
            offices.append({'address': address, 'lat': lat, 'lng': lng, 'city_country': city_country})
    return offices


def _vacancy_office_first(offices, location):
    """The model is asked to put the office nearest the vacancy first, but sometimes
    leads with the HQ (a Zurich vacancy got pinned to Palo Alto). An office in the
    vacancy's own city always wins."""
    city = _city(location)
    if not city:
        return offices
    in_city = [o for o in offices if city in _city(o.get('city_country')) or city in o['address'].lower()]
    return in_city + [o for o in offices if o not in in_city]


def _apply_offices(job_application, offices):
    offices = _vacancy_office_first(offices, job_application.location)
    if not offices:
        job_application.company_address = 'Адрес не найден'
        job_application.company_lat = None
        job_application.company_lng = None
        job_application.company_offices = []
        return
    primary = offices[0]
    job_application.company_address = primary['address']
    job_application.company_lat = primary['lat']
    job_application.company_lng = primary['lng']
    job_application.company_offices = [{'address': o['address'], 'lat': o['lat'], 'lng': o['lng']} for o in offices]
    # Only overwrite the vacancy's location with the researched address for direct
    # employers - for agencies, the researched address is the agency's own HQ, not
    # where the actual end-client job is based.
    if primary.get('city_country') and not job_application.is_agency:
        job_application.location = primary['city_country']


def _research_fresh(job_application, location):
    """The three web searches are independent - run them concurrently (the slowest,
    research, takes ~25 s; sequentially they took ~55 s)."""
    name = job_application.company_name
    kwargs = {'job_application': job_application}
    research, reviews, facts = run_parallel(
        (get_company_research, (name, location), kwargs),
        (get_company_reviews, (name, location), kwargs),
        (get_company_facts, (name, location), kwargs),
    )
    # A single :online search on the main research call rarely surfaces
    # Kununu/Glassdoor pages - dedicated call with a review-site-targeted query.
    job_application.company_research = _replace_research_section(research, REVIEWS_HEADER, REPUTATION_HEADER, reviews)
    job_application.company_employees = extract_company_employees(job_application.company_research)
    if facts.website and not job_application.company_website:
        job_application.company_website = facts.website
    _apply_offices(job_application, _geocode_offices(facts.addresses))


def _research_from_cache(job_application, donor, location):
    job_application.company_research = donor.company_research
    job_application.company_employees = donor.company_employees
    job_application.company_website = job_application.company_website or donor.company_website
    if _city(location) == _city(donor.location) or not donor.company_offices:
        offices = [dict(o, city_country=None) for o in (donor.company_offices or [])]
        job_application.company_address = donor.company_address
        if offices:
            _apply_offices(job_application, offices)
        return
    # Different vacancy location - office order (nearest first) and map pin depend on it.
    facts = get_company_facts(job_application.company_name, location, job_application=job_application)
    _apply_offices(job_application, _geocode_offices(facts.addresses))


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


def _apply_ats(job_application, result):
    score = result.get('score')
    verdict = result.get('verdict', '')
    matched = _bullets(result.get('matched_skills', [])) or 'Нет'
    missing = _bullets(result.get('missing_skills', [])) or 'Нет'
    hard_filters = _bullets(result.get('hard_filter_risks', [])) or 'Не выявлено'

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
    # Tier signals re-derived with full context (vacancy + company research),
    # overriding the parse-time guess.
    job_application.has_growth_signal = result['has_growth_signal']
    job_application.has_toxic_flag = result['has_toxic_flag']
    job_application.has_overregulated_flag = result['has_overregulated_flag']
    job_application.german_blocks_daily_work = result['german_blocks_daily_work']


def research_job_application(job_application, cv_summary, use_cache=True):
    """Company research -> ATS score for one vacancy, saved in place. Research comes
    first so the ATS score can account for company size/type (build_company_profile).

    Returns 'cached' or 'fresh' (which research path was used). Raises on LLM/provider
    errors - callers decide whether one failure stops a batch."""
    location = job_application.location or ''
    donor = find_cached_research(job_application) if use_cache else None
    if donor is not None:
        _research_from_cache(job_application, donor, location)
    else:
        _research_fresh(job_application, location)

    result = get_ats_score(
        cv_summary, build_job_summary(job_application), build_company_profile(job_application),
        job_application=job_application,
    )
    if 'error' in result:
        print(f"ATS score failed for job_application id={job_application.pk}: {result['error']}")
    else:
        _apply_ats(job_application, result)

    job_application.save(update_fields=RESEARCH_FIELDS)
    return 'cached' if donor is not None else 'fresh'
