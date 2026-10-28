"""Offline tests for the job-application LLM pipeline: a fake OpenAI client stands in
for OpenRouter, so these run in `make test` without network or API spend. Live-model
smoke tests stay in tests.py (pytest, not collected by `make test`)."""
import contextvars
import json
from types import SimpleNamespace
from unittest import mock

from django.test import TestCase

from mysite.job_application import parser, services
from mysite.job_application.models import JobApplication, LLMCallLog

EXTRACTION = {
    "job_title": "AI-инженер", "company_name": "Acme AG", "location": ["Zürich"], "work_mode": "hybrid",
    "is_agency": False, "contact_person": "", "skills": ["Python", "LangChain"], "soft_skills": ["коммуникация"],
    "required_experience": "3+ года", "salary_range": "", "vacation_days": "", "other_benefits": ["Обучение"],
    "language": "en", "german": "", "minuses": [], "info": "Швейцарская компания", "application_instructions": "",
    "is_switzerland": True, "has_growth_signal": True, "has_toxic_flag": False, "has_overregulated_flag": False,
    "german_blocks_daily_work": False,
}
ATS = {
    "score": 7, "verdict": "Подходит", "hard_filter_risks": [], "company_fit_note": "Средняя компания.",
    "matched_skills": ["Python"], "missing_skills": [], "summary": "Хорошо подходит.",
    "has_growth_signal": False, "has_toxic_flag": True, "has_overregulated_flag": False,
    "german_blocks_daily_work": False,
}
RESEARCH = (
    "Компания: Acme AG\nГод основания: 2001\nЧисло сотрудников:\n120 ([acme.ch](https://acme.ch/about)).\n\n"
    "Известные клиенты / проекты: UBS\n"
    "Отзывы сотрудников (Glassdoor, Kununu, Indeed и т.п.): old\n"
    "Общая репутация и красные флаги: нет\nAI-направление компании: есть"
)


class FakeClient:
    """Answers by the request's structured-output schema name or system prompt."""

    def __init__(self, overrides=None):
        self.calls = []
        self.overrides = overrides or {}
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def _answer(self, kwargs):
        schema = (kwargs.get('response_format') or {}).get('json_schema', {}).get('name')
        system = kwargs['messages'][0]['content']
        if schema in self.overrides:
            return self.overrides[schema]
        if schema == 'vacancy_extraction':
            return json.dumps(EXTRACTION)
        if schema == 'ats_score':
            return json.dumps(ATS)
        if schema == 'company_facts':
            return json.dumps({"website": "https://www.acme.ch/", "addresses": ["Bahnhofstrasse 1, 8001 Zürich, Switzerland"]})
        if schema == 'cover_letter_parts':
            return json.dumps({"greeting": "Dear team,", "middle_paragraph": "Middle.", "closing_paragraph": "Closing."})
        if 'translator' in system:
            return "Перевод вакансии"
        if 'Kununu, Glassdoor, Indeed' in system:
            return "Kununu 4.1 (Kununu)"
        if 'evaluate a company' in system:
            return RESEARCH
        return "Intro text"

    def create(self, **kwargs):
        self.calls.append(kwargs)
        content = self._answer(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason='stop')],
            usage=None, model=kwargs['model'],
        )

    def schemas(self):
        return [(c.get('response_format') or {}).get('json_schema', {}).get('name') for c in self.calls]


def sequential(*calls):
    return [fn(*args, **kwargs) for fn, args, kwargs in calls]


class PipelineTestCase(TestCase):
    def setUp(self):
        self.client_fake = FakeClient()
        patches = [
            mock.patch.object(parser, 'get_client', lambda: self.client_fake),
            # Worker threads would need their own connection to the per-test transaction.
            mock.patch.object(services, 'run_parallel', sequential),
            mock.patch.object(services, 'geocode_address', lambda address: (47.37, 8.54, 'Zürich, Switzerland')),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)


class SkillsFormattingTests(PipelineTestCase):
    def test_as_lines_accepts_stored_string_and_list(self):
        self.assertEqual(parser.as_lines('Python, LangChain,  RAG'), 'Python\nLangChain\nRAG')
        self.assertEqual(parser.as_lines(['Python', ' RAG ']), 'Python\nRAG')
        self.assertEqual(parser.as_lines(''), '')

    def test_cv_intro_prompt_lists_skills_not_characters(self):
        # regenerate_all passes JobApplication.key_skills - a ', '-joined string.
        parser.get_cv_intro(None, 'Sample intro', 'en', 'Agentic AI, Python', 'Teamwork')
        user_prompt = self.client_fake.calls[0]['messages'][1]['content']
        self.assertIn('Agentic AI\nPython', user_prompt)
        self.assertNotIn('A\ng\ne', user_prompt)

    def test_cover_letter_prompt_lists_skills_not_characters(self):
        letter = parser.get_cover_letter(None, 'AI Engineer', 'Acme', '', 'en', 'Python, RAG', 'Teamwork', '')
        user_prompt = self.client_fake.calls[0]['messages'][1]['content']
        self.assertIn('Python\nRAG', user_prompt)
        self.assertTrue(letter.startswith('Dear team,'))


class ChatCompletionTests(PipelineTestCase):
    def test_profile_defaults_and_native_fallback_routing(self):
        parser.get_ats_score('cv', 'job')
        call = self.client_fake.calls[0]
        self.assertEqual(call['model'], parser.ATS_MODEL)
        self.assertEqual(call['extra_body']['models'], [parser.ATS_MODEL, parser.ATS_MODEL_FALLBACK])
        self.assertNotIn('provider', call['extra_body'])

    def test_explicit_kwargs_win_over_profile(self):
        parser.chat_completion(
            self.client_fake, purpose='ats_score', model='other/model', temperature=0.9,
            messages=[{"role": "user", "content": "x"}],
        )
        call = self.client_fake.calls[0]
        self.assertEqual((call['model'], call['temperature']), ('other/model', 0.9))
        self.assertNotIn('models', call['extra_body'])

    def test_invalid_structured_output_is_an_error_not_defaults(self):
        self.client_fake.overrides['ats_score'] = json.dumps({"score": 7})
        self.assertIn('error', parser.get_ats_score('cv', 'job'))

    def test_tracked_calls_are_collected(self):
        with parser.track_llm_calls() as ids:
            parser.get_full_text_ru('text')
        self.assertEqual(len(ids), 1)
        self.assertEqual(LLMCallLog.objects.get(pk=ids[0]).purpose, 'full_text_translation')


class ExtractEmployeesTests(TestCase):
    def test_section_value_without_links(self):
        self.assertEqual(parser.extract_company_employees(RESEARCH), '120 (acme.ch)')

    def test_not_found(self):
        self.assertEqual(parser.extract_company_employees('Число сотрудников: Информация не найдена.\n\n'),
                         parser.EMPLOYEES_NOT_FOUND)
        self.assertEqual(parser.extract_company_employees('no sections'), parser.EMPLOYEES_NOT_FOUND)


class IngestTests(PipelineTestCase):
    def test_creates_job_and_attaches_only_its_own_calls(self):
        foreign = LLMCallLog.objects.create(purpose='concurrent_run', model='x')

        result = services.ingest_vacancy('Vacancy text', url='https://example.com/job/1')

        job = result.job_application
        self.assertEqual((job.company_name, job.key_skills, job.location), ('Acme AG', 'Python, LangChain', 'Zürich'))
        self.assertEqual(job.full_text_ru, 'Перевод вакансии')
        self.assertEqual(set(job.llm_calls.values_list('purpose', flat=True)), {'extract_vacancy', 'full_text_translation'})
        foreign.refresh_from_db()
        self.assertIsNone(foreign.job_application)

    def test_relevance_gate_runs_before_any_extraction(self):
        with mock.patch.object(services, 'check_vacancy_relevance', return_value=0.1):
            result = services.ingest_vacancy('Vacancy text', cv_summary='cv', relevance_threshold=0.25)
        self.assertIsNone(result.job_application)
        self.assertTrue(result.skip_reason.startswith('not relevant'))
        self.assertEqual(self.client_fake.calls, [])
        self.assertFalse(JobApplication.objects.exists())

    def test_failed_relevance_check_does_not_block(self):
        with mock.patch.object(services, 'check_vacancy_relevance', return_value=None):
            result = services.ingest_vacancy('Vacancy text', cv_summary='cv', relevance_threshold=0.25)
        self.assertIsNotNone(result.job_application)

    def test_unusable_extraction_raises(self):
        self.client_fake.overrides['vacancy_extraction'] = '{"job_title": "x"}'
        with self.assertRaises(services.VacancyIngestError):
            services.ingest_vacancy('Vacancy text')
        self.assertFalse(JobApplication.objects.exists())


class ResearchTests(PipelineTestCase):
    def make_job(self, **fields):
        defaults = dict(company_name='Acme AG', job_title='AI Engineer', location='Zürich', key_skills='Python')
        defaults.update(fields)
        return JobApplication.objects.create(**defaults)

    def test_fresh_research_fills_company_fields_and_ats(self):
        job = self.make_job()
        self.assertEqual(services.research_job_application(job, 'cv'), 'fresh')

        job.refresh_from_db()
        self.assertIn('Kununu 4.1', job.company_research)
        self.assertNotIn(': old', job.company_research)
        self.assertEqual(job.company_employees, '120 (acme.ch)')
        self.assertEqual(job.company_website, 'acme.ch')
        self.assertEqual(job.company_address, 'Bahnhofstrasse 1, 8001 Zürich, Switzerland')
        self.assertEqual((job.ats_score, job.has_toxic_flag), (7, True))
        self.assertNotIn('company_employees', self.client_fake.schemas())

    def test_same_company_reuses_recent_research(self):
        donor = self.make_job()
        services.research_job_application(donor, 'cv')
        donor.refresh_from_db()
        self.client_fake.calls.clear()

        job = self.make_job(job_title='Data Engineer')
        self.assertEqual(services.research_job_application(job, 'cv'), 'cached')

        job.refresh_from_db()
        self.assertEqual(job.company_research, donor.company_research)
        self.assertEqual(job.company_offices, donor.company_offices)
        self.assertEqual(self.client_fake.schemas(), ['ats_score'])

    def test_cache_refetches_offices_for_another_location(self):
        donor = self.make_job()
        services.research_job_application(donor, 'cv')
        self.client_fake.calls.clear()

        job = self.make_job(location='Basel')
        services.research_job_application(job, 'cv')
        self.assertEqual(self.client_fake.schemas(), ['company_facts', 'ats_score'])

    def test_use_cache_false_forces_fresh_search(self):
        services.research_job_application(self.make_job(), 'cv')
        job = self.make_job()
        self.assertEqual(services.research_job_application(job, 'cv', use_cache=False), 'fresh')


class OfficeOrderTests(TestCase):
    def test_office_in_vacancy_city_becomes_primary(self):
        offices = [
            {'address': '1 Main St, Palo Alto, CA, United States', 'city_country': 'Palo Alto, United States'},
            {'address': 'Bahnhofstrasse 1, 8001 Zürich, Switzerland', 'city_country': 'Zürich, Switzerland'},
        ]
        ordered = services._vacancy_office_first(offices, 'Zurich, Zurich, Switzerland')
        self.assertEqual(ordered[0]['city_country'], 'Zürich, Switzerland')
        self.assertEqual(services._vacancy_office_first(offices, ''), offices)


class RunParallelTests(TestCase):
    def test_results_in_order_and_context_propagates(self):
        var = contextvars.ContextVar('var', default='unset')
        var.set('outer')
        results = services.run_parallel(
            (lambda a: a * 2, (2,), {}),
            (var.get, (), {}),
        )
        self.assertEqual(results, [4, 'outer'])


class ViewTests(PipelineTestCase):
    def test_parse_text_extraction_error_returns_json(self):
        self.client_fake.overrides['vacancy_extraction'] = 'not json'
        response = self.client.post('/api/parse_text/', data=json.dumps({'text': 'x'}), content_type='application/json')
        self.assertEqual(response.status_code, 502)
        self.assertIn('error', response.json())

    def test_parse_text_success(self):
        response = self.client.post('/api/parse_text/', data=json.dumps({'text': 'x'}), content_type='application/json')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['company_name'], 'Acme AG')
