"""
Pull LinkedIn job-alert emails from Gmail, extract vacancy links, and for each
new one: parse the real vacancy text (same extraction the manual "Parse URL"
admin flow uses), create a JobApplication, and run company research + ATS
scoring - unless it's a still-unverified agency listing (matches the
research_company admin action's own skip rule) or --skip-scoring is passed.

Requires a one-time OAuth setup first: poetry run python scripts/gmail_oauth_setup.py

Usage:
    poetry run python manage.py import_gmail_vacancies [--days 7] [--limit 20]
        [--dry-run] [--skip-scoring]
"""
import os
import re
import time
import traceback

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone
import email.utils as email_utils

from mysite.job_application.admin import research_company
from mysite.job_application.models import JobApplication, LLMCallLog
from mysite.job_application.parser import (
    get_company_website, get_full_text_ru, get_job_text, get_openai_response, process_json,
)

TOKEN_PATH = os.path.join(settings.BASE_DIR, 'token_gmail.json')
SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']

# LinkedIn sends job alerts from a few different addresses depending on the
# notification type (single-job alert, "similar jobs" digest, multi-job digest).
SENDERS = ['jobalerts-noreply@linkedin.com', 'jobs-noreply@linkedin.com', 'messages-noreply@linkedin.com']

JOB_ID_RE = re.compile(r'linkedin\.com/(?:comm/)?jobs/view/(\d+)')


class DummyModelAdmin:
    """Stand-in for the admin ModelAdmin instance research_company() expects -
    only message_user() is actually called, to report skipped agency listings."""

    def __init__(self, stdout):
        self.stdout = stdout

    def message_user(self, request, msg, level=None):
        self.stdout.write(f'    [info] {msg}')


def get_gmail_service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    if not os.path.exists(TOKEN_PATH):
        raise RuntimeError(
            f"{TOKEN_PATH} not found. Run: poetry run python scripts/gmail_oauth_setup.py"
        )
    creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(TOKEN_PATH, 'w') as f:
            f.write(creds.to_json())
    return build('gmail', 'v1', credentials=creds)


def extract_job_ids_and_date(service, message_id):
    """Returns (set_of_job_ids, email_datetime)."""
    import base64

    full = service.users().messages().get(userId='me', id=message_id, format='full').execute()

    def walk_for_html_and_text(payload):
        html, text = None, None
        if payload.get('body', {}).get('data'):
            data = base64.urlsafe_b64decode(payload['body']['data']).decode('utf-8', errors='replace')
            if payload.get('mimeType') == 'text/html':
                html = data
            elif payload.get('mimeType') == 'text/plain':
                text = data
        for part in payload.get('parts', []) or []:
            h, t = walk_for_html_and_text(part)
            html = html or h
            text = text or t
        return html, text

    html, text = walk_for_html_and_text(full['payload'])
    body = (html or '') + (text or '')
    ids = set(JOB_ID_RE.findall(body))

    headers = {h['name']: h['value'] for h in full['payload'].get('headers', [])}
    date_str = headers.get('Date')
    email_dt = None
    if date_str:
        try:
            parsed = email_utils.parsedate_to_datetime(date_str)
            if parsed.tzinfo is None:
                parsed = timezone.make_aware(parsed)
            email_dt = parsed
        except (TypeError, ValueError):
            email_dt = None
    return ids, email_dt or timezone.now()


def existing_job_ids():
    ids = set()
    for url in JobApplication.objects.exclude(url__isnull=True).exclude(url='').values_list('url', flat=True):
        m = JOB_ID_RE.search(url) or re.search(r'/jobs/view/(\d+)', url)
        if m:
            ids.add(m.group(1))
    return ids


def parse_and_create_vacancy(url, email_dt, stdout):
    """Mirrors ParseURLView's extraction, but returns the created JobApplication
    instead of an HTTP response, and doesn't generate cover letter / cv intro
    (same deliberate deferral ParseURLView itself documents)."""
    started_at = timezone.now()
    prompt_text = get_job_text(url)
    if not prompt_text or len(prompt_text) < 200:
        return None, 'empty or unreachable page'

    is_closed = 'No longer accepting applications' in prompt_text

    raw = get_openai_response(prompt_text)
    result = process_json(raw)
    if 'error' in result:
        return None, f'extraction error: {result}'

    company_name = result.get('company_name', '')
    location = ', '.join(result.get('location', []) or [])
    full_text_ru = get_full_text_ru(prompt_text)
    company_website = get_company_website(company_name, location)

    job_application = JobApplication(
        company_name=company_name,
        job_title=result.get('job_title', ''),
        url=url,
        location=location,
        work_mode=result.get('work_mode', 'office'),
        is_agency=result.get('is_agency', False),
        contact_person=result.get('contact_person', ''),
        key_skills=', '.join(result.get('skills', []) or []),
        soft_skills=', '.join(result.get('soft_skills', []) or []),
        required_experience=result.get('required_experience', ''),
        salary_range=result.get('salary_range', ''),
        vacation_days=result.get('vacation_days', ''),
        other_benefits='\n'.join(f"- {b}" for b in (result.get('other_benefits', []) or [])),
        minuses='\n'.join(f"- {m}" for m in (result.get('minuses', []) or [])),
        language=result.get('language', ''),
        german_level=result.get('german', ''),
        info=f"Источник: Gmail import, письмо от {email_dt:%Y-%m-%d}",
        full_text_ru=full_text_ru,
        company_website=company_website,
        application_instructions=result.get('application_instructions', ''),
        is_switzerland=result.get('is_switzerland', False),
        has_growth_signal=result.get('has_growth_signal', False),
        has_toxic_flag=result.get('has_toxic_flag', False),
        has_overregulated_flag=result.get('has_overregulated_flag', False),
        german_blocks_daily_work=result.get('german_blocks_daily_work', False),
        status='closed' if is_closed else 'draft',
    )
    job_application.save()
    JobApplication.objects.filter(pk=job_application.pk).update(date_added=email_dt)
    LLMCallLog.objects.filter(job_application__isnull=True, created_at__gte=started_at).update(
        job_application=job_application,
    )
    return job_application, None


class Command(BaseCommand):
    help = 'Import new vacancies from LinkedIn job-alert emails in Gmail, parse their real text, and score them.'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=7, help='How many days back to search Gmail (default 7).')
        parser.add_argument(
            '--limit', type=int, default=20,
            help='Max number of NEW vacancies to fully process this run (default 20, safety cap on LLM spend).',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Only report which vacancy links would be imported; no LLM calls, no DB writes.',
        )
        parser.add_argument(
            '--skip-scoring', action='store_true',
            help='Create vacancies with real parsed text but skip company research + ATS scoring.',
        )

    def handle(self, *args, **options):
        days = options['days']
        limit = options['limit']
        dry_run = options['dry_run']
        skip_scoring = options['skip_scoring']

        service = get_gmail_service()
        sender_clause = ' OR '.join(f'from:{s}' for s in SENDERS)
        query = f'({sender_clause}) newer_than:{days}d'
        self.stdout.write(f'Gmail query: {query}')

        msg_ids = []
        page_token = None
        while True:
            resp = service.users().messages().list(
                userId='me', q=query, maxResults=100, pageToken=page_token,
            ).execute()
            msg_ids.extend(m['id'] for m in resp.get('messages', []))
            page_token = resp.get('nextPageToken')
            if not page_token:
                break
        self.stdout.write(f'Found {len(msg_ids)} matching emails.')

        known_ids = existing_job_ids()
        new_jobs = {}  # job_id -> email_dt (earliest seen)
        for i, mid in enumerate(msg_ids, 1):
            ids, email_dt = extract_job_ids_and_date(service, mid)
            for jid in ids:
                if jid in known_ids:
                    continue
                if jid not in new_jobs or email_dt < new_jobs[jid]:
                    new_jobs[jid] = email_dt

        self.stdout.write(f'New, not-yet-in-DB vacancy links: {len(new_jobs)}')
        if dry_run:
            for jid, dt in sorted(new_jobs.items(), key=lambda kv: kv[1]):
                self.stdout.write(
                    f'  {jid}  (email {dt:%Y-%m-%d})  https://www.linkedin.com/jobs/view/{jid}/'
                )
            return

        if not new_jobs:
            return

        targets = sorted(new_jobs.items(), key=lambda kv: kv[1])[:limit]
        if len(new_jobs) > limit:
            self.stdout.write(f'Processing {limit} of {len(new_jobs)} (raise --limit to process more).')

        dummy_admin = DummyModelAdmin(self.stdout)

        created, scored, failed = 0, 0, []
        for n, (jid, email_dt) in enumerate(targets, 1):
            url = f'https://www.linkedin.com/jobs/view/{jid}/'
            self.stdout.write(f'--- [{n}/{len(targets)}] {jid} ---')
            t0 = time.time()
            try:
                job, err = parse_and_create_vacancy(url, email_dt, self.stdout)
                if err:
                    self.stdout.write(f'    SKIP: {err}')
                    failed.append(jid)
                    continue
                created += 1
                self.stdout.write(
                    f'    created id={job.pk} [{job.status}] {job.company_name} :: {job.job_title} '
                    f'({time.time() - t0:.1f}s)'
                )

                if skip_scoring or job.status != 'draft':
                    continue

                t1 = time.time()
                research_company(dummy_admin, None, JobApplication.objects.filter(pk=job.pk))
                job.refresh_from_db()
                if job.ats_score is not None:
                    scored += 1
                    self.stdout.write(
                        f'    scored: ats={job.ats_score} ({job.ats_verdict}) ({time.time() - t1:.1f}s)'
                    )
            except Exception as e:
                self.stdout.write(f'    FAILED after {time.time() - t0:.1f}s: {e!r}')
                traceback.print_exc()
                failed.append(jid)

        self.stdout.write(
            f'\n=== DONE: {created} created, {scored} scored, {len(failed)} failed: {failed} ==='
        )
