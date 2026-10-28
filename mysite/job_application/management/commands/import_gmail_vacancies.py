"""
Pull LinkedIn job-alert emails from Gmail, extract vacancy links, and for each
new one: check the raw page text is at least broadly relevant to the candidate's
CV (TypeSafe Jev, --relevance-threshold; clearly off-field vacancies are skipped
before any extraction spend and never saved), extract it (same services.ingest_vacancy
the "Parse URL" API uses), and run company research + ATS scoring - unless it's a
still-unverified agency listing (matches the research_company admin action's own
skip rule) or --skip-scoring is passed. Stops early once the run's LLM spend
reaches --max-cost-usd.

Requires a one-time OAuth setup first: poetry run python scripts/gmail_oauth_setup.py

Usage:
    poetry run python manage.py import_gmail_vacancies [--days 7] [--limit 20]
        [--dry-run] [--skip-scoring] [--relevance-threshold 0.25] [--max-cost-usd 10]
"""
import os
import re
import time
import traceback

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils import timezone
import email.utils as email_utils

from mysite.job_application.admin import build_cv_summary
from mysite.job_application.models import JobApplication
from mysite.job_application.parser import get_job_text
from mysite.job_application.services import ingest_vacancy, llm_cost_since, research_job_application

# Below this probability (Jev's "relevant" judgment, 0-1) a parsed vacancy is
# dropped instead of being saved - deliberately low, so only clearly-off-field
# or wildly-mismatched-seniority alerts get filtered, not just imperfect fits.
RELEVANCE_THRESHOLD = 0.25

# Hard stop for one run's LLM spend (all purposes, USD as reported by OpenRouter).
# A fully researched + scored vacancy costs ~$0.15, so the default covers --limit 40
# with headroom but stops a runaway (e.g. a retry storm) well before it gets expensive.
MAX_COST_USD = 10.0

TOKEN_PATH = os.path.join(settings.BASE_DIR, 'token_gmail.json')
SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']

# LinkedIn sends job alerts from a few different addresses depending on the
# notification type (single-job alert, "similar jobs" digest, multi-job digest).
SENDERS = ['jobalerts-noreply@linkedin.com', 'jobs-noreply@linkedin.com', 'messages-noreply@linkedin.com']

JOB_ID_RE = re.compile(r'linkedin\.com/(?:comm/)?jobs/view/(\d+)')


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


def parse_and_create_vacancy(url, email_dt, cv_summary, relevance_threshold, stdout):
    """Returns (JobApplication or None, skip reason). Same ingest as ParseURLView, plus
    the relevance gate and the email's own date as date_added."""
    prompt_text = get_job_text(url)
    if not prompt_text or len(prompt_text) < 200:
        return None, 'empty or unreachable page'

    is_closed = 'No longer accepting applications' in prompt_text
    result = ingest_vacancy(
        prompt_text, url=url, date_added=email_dt,
        status='closed' if is_closed else 'draft',
        info=f"Источник: Gmail import, письмо от {email_dt:%Y-%m-%d}",
        cv_summary=cv_summary, relevance_threshold=relevance_threshold,
    )
    if result.relevance is not None:
        stdout.write(f'    relevance (jev)={result.relevance:.2f}')
    if result.skip_reason:
        return None, result.skip_reason
    return result.job_application, None


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
        parser.add_argument(
            '--relevance-threshold', type=float, default=RELEVANCE_THRESHOLD,
            help=(
                f'Jev "relevant" probability (0-1) below which a vacancy is dropped instead of '
                f'saved (default {RELEVANCE_THRESHOLD}).'
            ),
        )
        parser.add_argument(
            '--max-cost-usd', type=float, default=MAX_COST_USD,
            help=f'Stop processing new vacancies once this run has spent this much on LLM calls (default {MAX_COST_USD}).',
        )

    def handle(self, *args, **options):
        days = options['days']
        limit = options['limit']
        dry_run = options['dry_run']
        skip_scoring = options['skip_scoring']
        relevance_threshold = options['relevance_threshold']
        max_cost_usd = options['max_cost_usd']

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

        run_started_at = timezone.now()
        cv_summary = build_cv_summary(get_user_model().objects.first())

        created, scored, skipped_irrelevant, failed = 0, 0, 0, []
        for n, (jid, email_dt) in enumerate(targets, 1):
            spent = llm_cost_since(run_started_at)
            if spent >= max_cost_usd:
                self.stdout.write(
                    f'BUDGET: spent ${spent:.2f} >= --max-cost-usd {max_cost_usd}, '
                    f'stopping with {len(targets) - n + 1} vacancies left for the next run.'
                )
                break
            url = f'https://www.linkedin.com/jobs/view/{jid}/'
            self.stdout.write(f'--- [{n}/{len(targets)}] {jid} ---')
            t0 = time.time()
            try:
                job, err = parse_and_create_vacancy(url, email_dt, cv_summary, relevance_threshold, self.stdout)
                if err:
                    self.stdout.write(f'    SKIP: {err}')
                    if err.startswith('not relevant'):
                        skipped_irrelevant += 1
                    else:
                        failed.append(jid)
                    continue
                created += 1
                self.stdout.write(
                    f'    created id={job.pk} [{job.status}] {job.company_name} :: {job.job_title} '
                    f'({time.time() - t0:.1f}s)'
                )

                if skip_scoring or job.status != 'draft':
                    continue
                if job.is_agency_unverified:
                    self.stdout.write('    [info] agency listing, end client unverified - research skipped')
                    continue

                t1 = time.time()
                path = research_job_application(job, cv_summary)
                if job.ats_score is not None:
                    scored += 1
                    self.stdout.write(
                        f'    scored: ats={job.ats_score} ({job.ats_verdict}), research={path} '
                        f'({time.time() - t1:.1f}s)'
                    )
            except Exception as e:
                self.stdout.write(f'    FAILED after {time.time() - t0:.1f}s: {e!r}')
                traceback.print_exc()
                failed.append(jid)

        self.stdout.write(
            f'\n=== DONE: {created} created, {scored} scored, '
            f'{skipped_irrelevant} skipped as irrelevant, {len(failed)} failed: {failed}, '
            f'LLM spend ${llm_cost_since(run_started_at):.2f} ==='
        )
