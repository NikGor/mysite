import contextlib
import contextvars
import json
import os
import re
import time
from dataclasses import dataclass
from typing import List, Literal, Optional, Tuple
from dotenv import load_dotenv
from langdetect import detect
from jinja2 import Environment, FileSystemLoader
from pydantic import BaseModel, ConfigDict, Field, ValidationError
import requests
import newspaper
import openai
from bs4 import BeautifulSoup

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DECISIONS_API_URL = "https://openrouter.ai/api/alpha/decisions"
RELEVANCE_MODEL = "typesafe/jev-1.14"
# gpt-6-luna is the default workhorse: structured outputs + reasoning, and the cheapest
# capable model on OpenRouter ($0.10/$0.50 per 1M tokens in/out as of 2026-10).
MODEL = "openai/gpt-6-luna"
MODEL_FALLBACK = "openai/gpt-5.6-luna"
EXTRACTION_MODEL = MODEL
# ATS scoring is the one reasoning-heavy judgment, and tiers compare scores across
# months - so it needs a calibrated, critical model. Side by side on the same vacancies
# (2026-10-28) gpt-6-luna and gpt-6.1-terra scored 2-3 points above the Opus 4.8
# baseline, Opus 5.5 stayed within 1 point (and listed the most hard-filter risks).
ATS_MODEL = "anthropic/claude-opus-5.5"
ATS_MODEL_FALLBACK = "openai/gpt-6-sol"
# Cover letters stay on Sonnet: the one place where writing style matters more than price.
COVER_LETTER_MODEL = "anthropic/claude-sonnet-5"
# ':online' = OpenRouter web-search plugin on top of the model.
RESEARCH_MODEL = f"{MODEL}:online"
ADDRESS_MODEL = f"{MODEL}:online"

HTTP_TIMEOUT_S = 20


@dataclass(frozen=True)
class LLMProfile:
    """Per-purpose call defaults - the single place to change a model or its params.
    `fallbacks` go to OpenRouter's native `models` routing: it switches model on any
    provider error (not just one exception type), within the same request."""
    model: str
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    fallbacks: Tuple[str, ...] = ()
    reasoning_effort: Optional[Literal['minimal', 'low', 'medium', 'high']] = None


LLM_PROFILES = {
    'extract_vacancy': LLMProfile(
        EXTRACTION_MODEL, temperature=0.2, max_tokens=6000, fallbacks=(MODEL_FALLBACK,), reasoning_effort='low',
    ),
    # Long vacancies used to hit the old 4096 cap and come back truncated.
    'full_text_translation': LLMProfile(MODEL, temperature=0.2, max_tokens=8192, fallbacks=(MODEL_FALLBACK,)),
    'cv_intro': LLMProfile(MODEL, temperature=0.6, max_tokens=1024, fallbacks=(MODEL_FALLBACK,)),
    'cover_letter': LLMProfile(COVER_LETTER_MODEL, temperature=0.8, max_tokens=4000, fallbacks=(MODEL,)),
    'cover_letter_fallback': LLMProfile(MODEL, temperature=0.8, max_tokens=1500),
    'company_research': LLMProfile(RESEARCH_MODEL, temperature=0.3, max_tokens=4000, fallbacks=(f"{MODEL_FALLBACK}:online",)),
    # Reasoning tokens count against max_tokens - 2000 was hit in practice.
    'company_reviews': LLMProfile(RESEARCH_MODEL, temperature=0.3, max_tokens=4000),
    'company_facts': LLMProfile(ADDRESS_MODEL, temperature=0.2, max_tokens=1500),
    'company_website': LLMProfile(ADDRESS_MODEL, temperature=0.2, max_tokens=500),
    # Reasoning tokens of the fallback count against max_tokens - leave headroom.
    'ats_score': LLMProfile(ATS_MODEL, temperature=0.2, max_tokens=4000, fallbacks=(ATS_MODEL_FALLBACK,)),
}

# Collects LLMCallLog ids created inside track_llm_calls(), so a pipeline can attach
# them to the JobApplication it creates afterwards. Replaces the old "every
# unattached log since start time" query, which grabbed calls of concurrent runs.
_tracked_llm_call_ids = contextvars.ContextVar('tracked_llm_call_ids', default=None)


@contextlib.contextmanager
def track_llm_calls():
    ids = []
    token = _tracked_llm_call_ids.set(ids)
    try:
        yield ids
    finally:
        _tracked_llm_call_ids.reset(token)


def attach_llm_calls(call_ids, job_application):
    from mysite.job_application.models import LLMCallLog
    LLMCallLog.objects.filter(pk__in=call_ids, job_application__isnull=True).update(job_application=job_application)


def _log_llm_call(**fields):
    try:
        from mysite.job_application.models import LLMCallLog
        log = LLMCallLog.objects.create(**fields)
    except Exception as e:
        print(f"Failed to log LLM call for {fields.get('purpose')}: {e}")
        return None
    tracked = _tracked_llm_call_ids.get()
    if tracked is not None:
        tracked.append(log.pk)
    return log


PROMPTS_DIR = os.path.join(os.path.dirname(__file__), 'prompts')
_jinja_env = Environment(
    loader=FileSystemLoader(PROMPTS_DIR),
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=False,
)


def render_prompt(template_name, **context):
    return _jinja_env.get_template(template_name).render(**context).strip()


# Typographic characters LLMs commonly produce that don't appear on a standard keyboard —
# replaced with their plain-keyboard equivalents so generated text doesn't read as AI-written.
_TEXT_SANITIZE_MAP = {
    # em/en dashes and minus sign -> plain hyphen
    '‐': '-', '‑': '-', '‒': '-', '–': '-', '—': '-', '―': '-', '−': '-',
    # bullet characters -> plain hyphen
    '•': '-', '●': '-', '▪': '-', '‣': '-', '◦': '-', '⁃': '-', '∙': '-',
    # ellipsis -> three dots
    '…': '...',
    # curly apostrophes -> straight apostrophe (keeps contractions/possessives working)
    '‘': "'", '’': "'",
    # double quotes (curly, straight, guillemets) -> removed entirely, no quoting at all
    '“': '', '”': '', '„': '', '‟': '', '«': '', '»': '',
    '‹': '', '›': '', '"': '',
}


def sanitize_llm_text(text):
    if not text:
        return ''
    for src, dst in _TEXT_SANITIZE_MAP.items():
        text = text.replace(src, dst)
    text = re.sub(r'[ \t]{2,}', ' ', text)
    return text.strip()


LANGUAGE_NAMES = {
    'en': 'english',
    'de': 'deutsch',
}


class TierSignals(BaseModel):
    """Inputs of JobApplication.tier. Judged twice: from the vacancy text at parse time,
    then again in the ATS call with company research added (that verdict wins)."""
    model_config = ConfigDict(extra='forbid')

    has_growth_signal: bool = Field(
        description=(
            "True if the vacancy (or company research, if given) signals company growth/stability: a funding "
            "round or raised capital ('$X funding', 'Series A/B'), a growing team, hiring for multiple similar "
            "roles, named well-known clients, or explicit YoY growth figures ('N% growth'). False if no such "
            "signal is present."
        )
    )
    has_toxic_flag: bool = Field(
        description=(
            "True if the vacancy (or company research, if given) signals a toxic/high-pressure culture or other "
            "red-flag workplace-culture signals: phrases like 'top-1% performer', 'extreme ownership', '10x "
            "engineer', expectations of long hours/unlimited availability, similar hustle-culture language, "
            "employee reviews about burnout/overtime, notably low salary for the role's scope or minimal/no "
            "raises, or petty/rigid workplace rules (e.g. mandatory kitchen-cleaning duty rosters, strict "
            "clocking-in requirements). False if the tone is ordinary/professional."
        )
    )
    has_overregulated_flag: bool = Field(
        description=(
            "True if EITHER: (a) the text explicitly frames regulation as limiting day-to-day engineering work, "
            "e.g. 'highly regulated industry limits experimentation', 'strict compliance processes' as a stated "
            "constraint; OR (b) the company is in an explicitly regulated industry (bank, insurance, healthcare, "
            "government/public sector) AND the vacancy/research mentions governance/compliance/audit/"
            "risk-management processes as part of the day-to-day work (e.g. 'responsible AI', 'audit trail', "
            "'approval process', 'risk committee sign-off', 'governance framework'). Job ads never self-admit "
            "that bureaucracy slows them down, so criterion (b) is the realistic signal for most banking/"
            "insurance/healthcare/public-sector postings. False only when neither (a) nor this regulated-industry "
            "+ governance-language combination is present - a regulated industry alone is not enough."
        )
    )
    german_blocks_daily_work: bool = Field(
        description=(
            "True if German is required for actual day-to-day work (daily stand-ups, documentation, or "
            "communication with colleagues/clients in German), not just a nice-to-have or a formality. False if "
            "English is the working language, or German is only mentioned as a plus."
        )
    )


class VacancyExtraction(TierSignals):
    job_title: str = Field(description="Job title, in Russian")
    company_name: str = Field(description="Company name, unchanged (proper noun)")
    location: List[str] = Field(description="List of locations; empty if fully remote")
    work_mode: Literal["remote", "hybrid", "office"] = Field(
        description=(
            "'remote' if the vacancy explicitly offers fully remote/home-office work with no required office "
            "presence. 'hybrid' if it mentions a mix (e.g. '2 days in office', 'flexible/hybrid working', "
            "partial remote). 'office' if it requires on-site presence, or doesn't mention remote/hybrid work "
            "at all (default assumption for ordinary office-based roles)."
        )
    )
    is_agency: bool = Field(
        description=(
            "True ONLY if the posting makes clear that the candidate's actual day-to-day employer would be a "
            "DIFFERENT, specific company than the one posting the ad: a staffing/recruitment agency, headhunter "
            "or job platform staffing one particular end client for this one role. False if the posting company "
            "is the real employer - including IT/AI/data consultancies that describe their own ongoing 'clients' "
            "(plural). See the system prompt for the reliable signals."
        )
    )
    contact_person: str = Field(description="Contact person, unchanged (proper noun); empty if none")
    skills: List[str] = Field(description="Technical skills mentioned in the vacancy, unchanged (original tech terms)")
    soft_skills: List[str] = Field(description="Most likely required soft skills given the context, in Russian")
    required_experience: str = Field(description="Required experience, in Russian")
    salary_range: str = Field(description="Salary range if mentioned, in Russian; empty if not mentioned")
    vacation_days: str = Field(description="Vacation days if mentioned, in Russian; empty if not mentioned")
    other_benefits: List[str] = Field(description="Other benefits mentioned in the vacancy, in Russian")
    language: Literal["en", "de"] = Field(description="Actual language of the source vacancy text")
    german: str = Field(description="Most likely required level of German proficiency, A1 to C2; empty if not applicable")
    minuses: List[str] = Field(
        description=(
            "Potential negatives of the vacancy, mentioned or implied (long hours, high pressure, limited growth, "
            "lack of benefits, ...), in Russian"
        )
    )
    info: str = Field(
        description=(
            "Short info about the company from your own knowledge, in Russian: German or international, area of "
            "business, number of employees, headquarters city"
        )
    )
    application_instructions: str = Field(
        description=(
            "Any special instructions on HOW to apply, in Russian: whether a cover letter is explicitly "
            "not wanted (e.g. replaced by a short online questionnaire/form), the required application "
            "channel (e.g. only via a specific online link/portal, not email), application deadline, "
            "desired start date, or an explicit statement that agency/recruiter submissions are not "
            "accepted for this role. Empty string if the vacancy states nothing unusual beyond a normal "
            "application."
        )
    )
    is_switzerland: bool = Field(
        description=(
            "True if the vacancy's work location is in Switzerland - infer this from any Swiss city/canton "
            "name (e.g. Zurich, Zuerich, Winterthur, Bern, Basel, Geneva, Lausanne, Zug, Lucerne, Aargau, "
            "St. Gallen), CHF/Swiss franc salary figures, or an explicit country mention (Switzerland/Schweiz/ "
            "Suisse/Svizzera), even if the word 'Switzerland' itself never appears in the text. False for "
            "fully remote roles with no Swiss location signal, or roles located elsewhere."
        )
    )


class ATSScoreResult(TierSignals):
    score: int = Field(
        description=(
            "0-10 realistic ATS-style score based on: keyword/semantic match (~45%), experience relevance (~30%), "
            "skills coverage (~15%), CV structure/completeness (~10%). Soft skills barely count - do not let them "
            "inflate the score."
        )
    )
    verdict: Literal["Подходит", "Условно подходит", "Не подходит"]
    hard_filter_risks: List[str] = Field(
        description=(
            "Binary/hard requirements from the vacancy that could cause automatic rejection BEFORE scoring even "
            "happens, regardless of overall fit - e.g. minimum years of experience, mandatory degree, required "
            "certification, work permit/visa, required language certificate. Empty list if none found or all "
            "clearly satisfied. In Russian."
        )
    )
    company_fit_note: str = Field(
        description=(
            "1-2 sentences in Russian contextualizing the score against the likely ATS pass threshold for this "
            "company's size/type: small company/startup (~10-50 employees) usually reviews everything above ~75%, "
            "mid-market (~50-500) reviews roughly the top 20-30% (~80%+ threshold), enterprise (500+) sees only "
            "the top tier (~85%+ threshold) due to high applicant volume. Staffing agencies typically forward to "
            "the end client's own ATS, so agency postings should be judged by the end client's likely size/type "
            "if inferable, otherwise treated as mid-market by default."
        )
    )
    matched_skills: List[str] = Field(description="Vacancy requirements clearly matched by the candidate's CV, in Russian")
    missing_skills: List[str] = Field(description="Vacancy requirements missing from the candidate's CV, in Russian")
    summary: str = Field(description="2-3 sentence explanation of the overall fit, in Russian")


class CoverLetterParts(BaseModel):
    model_config = ConfigDict(extra='forbid')

    greeting: str = Field(description="The greeting line only, e.g. 'Dear Mr. Smith,' or 'Sehr geehrte Damen und Herren,'")
    middle_paragraph: str = Field(
        description="Current relevant work matched to this vacancy, plus an honest, positive acknowledgment of one gap"
    )
    closing_paragraph: str = Field(
        description="How the candidate's experience is useful for this specific role and company"
    )


class CompanyFacts(BaseModel):
    model_config = ConfigDict(extra='forbid')

    website: str = Field(
        description=(
            "Bare domain of the company's own official corporate website, e.g. 'example.com' or 'example.co.ch' - "
            "no scheme, 'www.', path or markdown. Not a job board, LinkedIn page or directory listing. Empty "
            "string if no verifiable official website can be found."
        )
    )
    addresses: List[str] = Field(
        description=(
            "List of up to 4 known office addresses for the company, most relevant to the vacancy "
            "location first (e.g. the office nearest the vacancy's stated location, if known). Each address "
            "must include street + house number, postal code, city and country when available, e.g. "
            "'Bahnhofstrasse 1, 8001 Zurich, Switzerland'. If only a city/country is known for an office, "
            "include just that (e.g. 'Zurich, Switzerland'). Return a single-item list if only one office is "
            "known. Return an empty list if no verifiable office location can be found at all."
        )
    )


def pydantic_response_format(model, name):
    schema = model.model_json_schema()
    schema.pop('title', None)
    return {
        "type": "json_schema",
        "json_schema": {
            "name": name,
            "strict": True,
            "schema": schema,
        },
    }


def get_client():
    load_dotenv()
    return openai.OpenAI(
        api_key=os.getenv("OPENROUTER_API_KEY"),
        base_url=OPENROUTER_BASE_URL,
    )


def _apply_profile(purpose, kwargs, extra_body):
    """Fills model/params from LLM_PROFILES unless the caller passed them explicitly
    (e.g. retry_failed_llm_calls replays a logged call with its exact original params)."""
    profile = LLM_PROFILES.get(purpose)
    if profile is None:
        return
    kwargs.setdefault('model', profile.model)
    if profile.temperature is not None:
        kwargs.setdefault('temperature', profile.temperature)
    if profile.max_tokens is not None:
        kwargs.setdefault('max_tokens', profile.max_tokens)
    if profile.fallbacks and kwargs['model'] == profile.model:
        extra_body.setdefault('models', [profile.model, *profile.fallbacks])
    if profile.reasoning_effort:
        extra_body.setdefault('reasoning', {'effort': profile.reasoning_effort})
    # No provider.require_parameters: reasoning models (gpt-5-mini) reject `temperature`,
    # which OpenRouter otherwise drops silently - requiring all params leaves 0 endpoints.


def chat_completion(client, purpose, job_application=None, retry_of=None, **kwargs):
    """Wraps client.chat.completions.create() to log the full prompt/response, latency,
    token usage and cost (in USD, as reported by OpenRouter) to LLMCallLog, so both LLM
    spend and output quality can be traced per feature/job application.

    Model and params default to LLM_PROFILES[purpose]; explicit kwargs win."""
    extra_body = dict(kwargs.pop('extra_body', None) or {})
    extra_body.setdefault('usage', {'include': True})
    _apply_profile(purpose, kwargs, extra_body)

    log_fields = dict(
        purpose=purpose,
        model=kwargs.get('model', ''),
        messages=kwargs.get('messages'),
        temperature=kwargs.get('temperature'),
        max_tokens=kwargs.get('max_tokens'),
        response_format=kwargs.get('response_format'),
        job_application=job_application,
        retry_of=retry_of,
    )

    start = time.monotonic()
    try:
        response = client.chat.completions.create(extra_body=extra_body, **kwargs)
    except Exception as e:
        _log_llm_call(**log_fields, latency_ms=int((time.monotonic() - start) * 1000), error=str(e))
        raise
    latency_ms = int((time.monotonic() - start) * 1000)

    response_text = None
    finish_reason = None
    if getattr(response, 'choices', None):
        response_text = response.choices[0].message.content
        finish_reason = getattr(response.choices[0], 'finish_reason', None)

    usage = getattr(response, 'usage', None)
    usage_dict = {}
    if usage is not None:
        usage_dict = usage.model_dump() if hasattr(usage, 'model_dump') else dict(usage)

    # With `models` fallback routing the model that actually answered can differ
    # from the requested one - log the real one.
    log_fields['model'] = getattr(response, 'model', None) or log_fields['model']
    _log_llm_call(
        **log_fields,
        response_text=response_text,
        latency_ms=latency_ms,
        prompt_tokens=usage_dict.get('prompt_tokens'),
        completion_tokens=usage_dict.get('completion_tokens'),
        total_tokens=usage_dict.get('total_tokens'),
        cost=usage_dict.get('cost'),
        error=f"truncated: finish_reason={finish_reason}" if finish_reason == 'length' else None,
    )

    return response


def _response_content(response):
    if not getattr(response, 'choices', None):
        return None
    return response.choices[0].message.content


def get_html_content(url):
    try:
        response = requests.get(url, timeout=HTTP_TIMEOUT_S)
        response.raise_for_status()
        return response.text
    except requests.RequestException as e:
        print(f"Error fetching the URL {url}: {e}")
        return None


def is_valid_url(url):
    regex = re.compile(
        r'^(?:http|ftp)s?://'
        r'(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+(?:[A-Z]{2,6}\.?|[A-Z0-9-]{2,}\.?)'
        r'(?::\d+)?'
        r'(?:/?|[/?]\S+)$', re.IGNORECASE)
    return re.match(regex, url) is not None


def get_job_text(url):
    """Returns the page's visible text, or '' if the page could not be fetched."""
    html = get_html_content(url)
    if not html:
        return ''
    soup = BeautifulSoup(html, 'html.parser')
    return soup.get_text(separator=' ', strip=True)


def get_job_summary(url):
    if not is_valid_url(url):
        print("Invalid URL")
        return None
    try:
        article = newspaper.Article(url)
        article.download()
        article.parse()
        return article.text
    except Exception as e:
        print(f"Error downloading or parsing the article: {e}")
        return None


def detect_job_language(url):
    text = get_job_summary(url)
    try:
        return detect(text)
    except Exception as e:
        print(f"Error detecting language: {e}")
        return None


class LLMOutputError(Exception):
    """The LLM answered, but not with output matching the requested schema."""


def parse_structured(response, model_cls):
    """Validates a structured-output response against its Pydantic schema, so a malformed
    answer fails loudly instead of silently turning into .get() defaults."""
    content = _response_content(response)
    if not content:
        raise LLMOutputError(f"Empty response for {model_cls.__name__}")
    cleaned = content.replace("```json", "").replace("```", "").strip()
    try:
        return model_cls.model_validate_json(cleaned)
    except ValidationError as e:
        raise LLMOutputError(f"Invalid {model_cls.__name__}: {e}") from e


def _extraction_messages(prompt_text):
    return [
        {"role": "system", "content": render_prompt('vacancy_extraction_system.j2')},
        {"role": "user", "content": prompt_text},
    ]


def extract_vacancy(prompt_text, job_application=None):
    """Structured vacancy fields from raw vacancy text. Raises LLMOutputError."""
    response = chat_completion(
        get_client(), purpose='extract_vacancy', job_application=job_application,
        messages=_extraction_messages(prompt_text),
        response_format=pydantic_response_format(VacancyExtraction, "vacancy_extraction"),
    )
    return parse_structured(response, VacancyExtraction)


def get_openai_response(prompt_text, api_key=None, model_id=None):
    """Raw-JSON variant of extract_vacancy(), kept for ad-hoc scripts."""
    response = chat_completion(
        get_client(), purpose='extract_vacancy',
        messages=_extraction_messages(prompt_text),
        response_format=pydantic_response_format(VacancyExtraction, "vacancy_extraction"),
    )
    return _response_content(response)


def get_full_text_ru(prompt_text, api_key=None, job_application=None):
    response = chat_completion(
        get_client(), purpose='full_text_translation', job_application=job_application,
        messages=[
            {"role": "system", "content": render_prompt('full_text_translation_system.j2')},
            {"role": "user", "content": prompt_text},
        ],
    )
    return sanitize_llm_text(_response_content(response))


def process_json(response):
    if not response:
        return {"error": "Empty response from the API"}

    cleaned_response = response.replace("```json", "").replace("```", "").strip()

    try:
        result = json.loads(cleaned_response)
        return result
    except json.JSONDecodeError:
        return {"error": "Invalid response from the API"}


# Enough of a LinkedIn page for the title, company and requirements - the relevance
# gate needs the gist, not every benefit bullet.
RELEVANCE_TEXT_LIMIT = 6000


def check_vacancy_relevance(cv_summary, vacancy_text, job_application=None):
    """Cheap/fast relevance gate via TypeSafe Jev's Decisions API (structured
    yes/no judgment, not a chat completion) - run on the raw page text BEFORE the
    extraction call, so LinkedIn alerts completely outside the candidate's field
    cost neither extraction nor translation and never reach the DB. Deliberately
    separate from get_ats_score, which is a full-text fit assessment that only runs
    after a JobApplication already exists.

    Returns the probability (0-1) that the vacancy is relevant, or None if the
    call failed - callers should treat None as "couldn't judge, don't block it"."""
    api_key = os.getenv("OPENROUTER_API_KEY")
    body = {
        "model": RELEVANCE_MODEL,
        "state": {
            "candidate_profile": cv_summary,
            "vacancy_text": (vacancy_text or '')[:RELEVANCE_TEXT_LIMIT],
        },
        "questions": {
            "relevant": {
                "type": "noul",
                "instructions": (
                    "Is this vacancy a reasonable match worth adding to the candidate's "
                    "job-application tracker, given their CV? The vacancy text is scraped page "
                    "text and may contain unrelated page chrome - judge the vacancy itself."
                ),
                "criteria": {
                    "true": (
                        "The role is in the same general field as the candidate's background "
                        "(software/AI engineering), even if seniority or exact tech stack isn't a "
                        "perfect fit."
                    ),
                    "false": (
                        "The role is clearly outside the candidate's field entirely (e.g. an "
                        "unrelated profession or industry with no engineering component) or wildly "
                        "mismatched in seniority (e.g. an unrelated non-tech entry-level role, or an "
                        "executive role far above the candidate's track record)."
                    ),
                },
            },
        },
    }

    started_at = time.monotonic()
    try:
        response = requests.post(
            DECISIONS_API_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=body,
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        _log_llm_call(
            purpose='vacancy_relevance', model=RELEVANCE_MODEL, messages=body,
            latency_ms=int((time.monotonic() - started_at) * 1000), error=str(e), job_application=job_application,
        )
        return None
    latency_ms = int((time.monotonic() - started_at) * 1000)

    usage = data.get('usage', {}) or {}
    probability = data.get('answers', {}).get('relevant', {}).get('noul')

    _log_llm_call(
        purpose='vacancy_relevance', model=data.get('model', RELEVANCE_MODEL), messages=body,
        response_text=json.dumps(data.get('answers', {})), latency_ms=latency_ms,
        prompt_tokens=usage.get('input_tokens'), completion_tokens=usage.get('output_tokens'),
        total_tokens=(usage.get('input_tokens') or 0) + (usage.get('output_tokens') or 0),
        cost=usage.get('cost'), job_application=job_application,
    )

    return probability


def as_lines(items):
    """Skills arrive both as lists (fresh extraction) and as the ', '-joined strings
    stored on JobApplication. '\\n'.join() on a string splits it into single
    characters - which is exactly what cv_intro/cover_letter prompts used to get."""
    if isinstance(items, str):
        items = items.split(',')
    return '\n'.join(item.strip() for item in (items or []) if item and item.strip())


SWISS_WORK_PERMIT_NOTE = {
    'en': (
        "As a German citizen, I can work in Switzerland under the EU/EFTA free movement "
        "of persons agreement."
    ),
    'de': (
        "Als deutscher Staatsbürger kann ich im Rahmen des Freizügigkeitsabkommens zwischen der "
        "EU/EFTA und der Schweiz in der Schweiz arbeiten."
    ),
}


def get_cv_intro(api_key, sample, language, skills, soft_skills, is_switzerland=False, job_application=None):
    max_chars = len(sample) + 50

    system_prompt = render_prompt(
        'cv_intro_system.j2',
        max_chars=max_chars,
        sample_length=len(sample),
        language=language,
        language_name=LANGUAGE_NAMES.get(language, 'english'),
        sample=sample,
    )
    user_prompt = render_prompt(
        'cv_intro_user.j2',
        skills_formatted=as_lines(skills),
        soft_skills_formatted=as_lines(soft_skills),
    )

    response = chat_completion(
        get_client(), purpose='cv_intro', job_application=job_application,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )

    text = sanitize_llm_text(_response_content(response))
    if not text:
        return ''
    if is_switzerland:
        note = SWISS_WORK_PERMIT_NOTE.get(language, SWISS_WORK_PERMIT_NOTE['en'])
        text = f"{text} {note}"
    return text


WURTH_INTRO = {
    'en': (
        "I'm Nikolai, currently working as an AI Engineer at Würth IT, part of the Würth Group. "
        "Würth is a major international group with around 87,000 employees in more than 80 "
        "countries, best known as the world's market leader in fastening and assembly technology. "
        "As part of the AI engineering team, I contribute to PICO, Würth's flagship AI assistant "
        "used company-wide."
    ),
    'de': (
        "Ich bin Nikolai und arbeite derzeit als AI Engineer bei Würth IT, einem Unternehmen der "
        "Würth-Gruppe. Würth ist ein international tätiger Konzern mit rund 87.000 Mitarbeitenden "
        "in mehr als 80 Ländern und weltweiter Marktführer im Bereich Befestigungs- und "
        "Montagetechnik. Im Team AI Engineering arbeite ich an PICO, dem unternehmensweiten "
        "KI-Assistenten von Würth."
    ),
}

PICO_DIFFERENTIATOR = {
    'en': (
        "On PICO, I've developed core components and architected several of its sub-agents, "
        "including ones with Human-in-the-Loop mechanisms that safely perform write operations in "
        "productive SAP systems - a solution that was even featured on Tagesschau, Germany's main "
        "national news program, for saving field sales representatives up to 30 minutes per "
        "customer visit."
    ),
    'de': (
        "An PICO habe ich Kernkomponenten entwickelt und die Architektur mehrerer Sub-Agents "
        "verantwortet, darunter Agents mit Human-in-the-Loop-Mechanismen, die sichere "
        "Schreiboperationen in produktiven SAP-Systemen ermöglichen - eine Lösung, die sogar in der "
        "Tagesschau vorgestellt wurde, weil sie Außendienstmitarbeitern bis zu 30 Minuten pro "
        "Kundenbesuch einspart."
    ),
}

SIGNOFF = {'en': 'Best regards,', 'de': 'Mit freundlichen Grüßen,'}


def get_cover_letter(api_key, job_title, company_name, sample, language, skills, soft_skills, contact_person,
                     sender_name='', cv_intro='', ats_context='', company_context='', is_agency=False,
                     is_switzerland=False, job_application=None):
    client = get_client()
    sender_name = sender_name or 'Nikolai Gordienko'

    system_prompt = render_prompt(
        'cover_letter_system.j2',
        job_title=job_title,
        company_name=company_name,
        language=language,
        language_name=LANGUAGE_NAMES.get(language, 'english'),
        contact_person=contact_person.strip() if contact_person else '',
        cv_intro=cv_intro.strip() if cv_intro else '',
        sample=sample.strip() if sample else '',
        ats_context=ats_context.strip() if ats_context else '',
        company_context=company_context.strip() if company_context else '',
        is_agency=is_agency,
        wurth_intro=WURTH_INTRO.get(language, WURTH_INTRO['en']),
        pico_differentiator=PICO_DIFFERENTIATOR.get(language, PICO_DIFFERENTIATOR['en']),
    )
    user_prompt = render_prompt(
        'cover_letter_user.j2',
        skills_formatted=as_lines(skills),
        soft_skills_formatted=as_lines(soft_skills),
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    response_format = pydantic_response_format(CoverLetterParts, "cover_letter_parts")

    def assemble(parts):
        wurth_intro = WURTH_INTRO.get(language, WURTH_INTRO['en'])
        differentiator = PICO_DIFFERENTIATOR.get(language, PICO_DIFFERENTIATOR['en'])
        signoff = SIGNOFF.get(language, SIGNOFF['en'])
        closing = parts.closing_paragraph.strip()
        if is_switzerland:
            closing = f"{closing} {SWISS_WORK_PERMIT_NOTE.get(language, SWISS_WORK_PERMIT_NOTE['en'])}"
        return sanitize_llm_text('\n\n'.join([
            parts.greeting.strip(),
            wurth_intro,
            parts.middle_paragraph.strip(),
            differentiator,
            closing,
            f"{signoff}\n{sender_name}",
        ]))

    def is_complete(parts):
        return bool(parts.greeting.strip() and parts.middle_paragraph.strip() and parts.closing_paragraph.strip())

    # Provider errors fall back to MODEL inside the request (LLM_PROFILES 'fallbacks').
    # COVER_LETTER_MODEL also occasionally returns an empty completion with no error
    # (an overly strict content-safety filter tripped by phrasing like "write access to
    # production systems"), which no router sees - retry those, then ask MODEL directly.
    for attempt in range(3):
        if attempt > 0:
            time.sleep(1.5 * attempt)
        response = chat_completion(
            client, purpose='cover_letter', job_application=job_application,
            messages=messages, response_format=response_format,
        )
        try:
            parts = parse_structured(response, CoverLetterParts)
        except LLMOutputError:
            continue
        if is_complete(parts):
            return assemble(parts)

    response = chat_completion(
        client, purpose='cover_letter_fallback', job_application=job_application,
        messages=messages, response_format=response_format,
    )
    try:
        parts = parse_structured(response, CoverLetterParts)
    except LLMOutputError:
        return ''
    return assemble(parts) if is_complete(parts) else ''


def _company_query(company_name, location=''):
    return f"{company_name}" + (f", {location}" if location else "")


def _strip_markdown_links(text):
    return re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', text)


def get_company_research(company_name, location='', api_key=None, job_application=None):
    response = chat_completion(
        get_client(), purpose='company_research', job_application=job_application,
        messages=[
            {"role": "system", "content": render_prompt('company_research_system.j2')},
            {"role": "user", "content": f"Company to research: {_company_query(company_name, location)}"},
        ],
    )
    return sanitize_llm_text(_response_content(response)) or 'Информация не найдена'


def get_company_reviews(company_name, location='', job_application=None):
    """A single :online web search tends to spend its result budget on general company
    facts and rarely surfaces Kununu/Glassdoor pages, so this is a dedicated call with a
    query that explicitly targets review platforms (same rationale as splitting out
    get_company_facts)."""
    query = _company_query(company_name, location)
    response = chat_completion(
        get_client(), purpose='company_reviews', job_application=job_application,
        extra_body={'plugins': [{'id': 'web', 'max_results': 8}]},
        messages=[
            {"role": "system", "content": render_prompt('company_reviews_system.j2')},
            {"role": "user", "content": (
                f"Company: {query}. Search for \"{query} kununu\" and \"{query} glassdoor reviews\"."
            )},
        ],
    )
    return sanitize_llm_text(_response_content(response)) or 'Информация не найдена'


EMPLOYEES_SECTION_RE = re.compile(r'Число сотрудников:\s*(.*?)\s*(?:\n\s*\n|Известные клиенты|$)', re.DOTALL)
EMPLOYEES_NOT_FOUND = 'Информация не найдена'


def extract_company_employees(company_research_text):
    """The research prompt fixes a 'Число сотрудников:' section header, so the employee
    count is a plain section lookup - no LLM call needed (it used to be a gpt-5-nano
    call that copied the same section back, ~8 s per vacancy)."""
    match = EMPLOYEES_SECTION_RE.search(company_research_text or '')
    if not match:
        return EMPLOYEES_NOT_FOUND
    text = _strip_markdown_links(match.group(1))
    text = ' '.join(text.split()).rstrip('.').strip()
    if not text or text.lower().startswith(EMPLOYEES_NOT_FOUND.lower()):
        return EMPLOYEES_NOT_FOUND
    return text[:512]


def _clean_domain(domain):
    domain = sanitize_llm_text(domain or '')
    if not domain:
        return ''
    domain = _strip_markdown_links(domain.splitlines()[0].strip()).strip(' \t*_')
    domain = re.sub(r'^https?://', '', domain, flags=re.IGNORECASE)
    domain = domain.removeprefix('www.').rstrip('/')
    if not domain or 'не найден' in domain.lower() or ' ' in domain:
        return ''
    return domain


def _clean_address(address):
    address = sanitize_llm_text(address)
    if not address:
        return ''
    # Strip markdown links/citations the model may add despite instructions
    return _strip_markdown_links(address.splitlines()[0].strip()).strip(' \t*_')


def get_company_facts(company_name, location='', job_application=None):
    """Official website + office addresses in one web-search call: both used to be
    separate calls to the same model with the same query, and the web-search fee
    dominated their cost. Returns a CompanyFacts with cleaned values (empty on failure)."""
    response = chat_completion(
        get_client(), purpose='company_facts', job_application=job_application,
        messages=[
            {"role": "system", "content": render_prompt('company_facts_system.j2')},
            {"role": "user", "content": f"Company: {_company_query(company_name, location)}"},
        ],
        response_format=pydantic_response_format(CompanyFacts, "company_facts"),
    )
    try:
        facts = parse_structured(response, CompanyFacts)
    except LLMOutputError as e:
        print(f"company_facts: {e}")
        return CompanyFacts(website='', addresses=[])
    addresses = [a for a in (_clean_address(a) for a in facts.addresses) if a]
    return CompanyFacts(website=_clean_domain(facts.website), addresses=addresses)


def get_company_website(company_name, location='', api_key=None, job_application=None):
    """Website-only lookup, kept for ad-hoc scripts - the pipeline uses get_company_facts()."""
    response = chat_completion(
        get_client(), purpose='company_website', job_application=job_application,
        messages=[
            {"role": "system", "content": render_prompt('company_website_system.j2')},
            {"role": "user", "content": f"Company: {_company_query(company_name, location)}"},
        ],
    )
    return _clean_domain(_response_content(response))


def geocode_address(address):
    """Returns (lat, lon, city_country) - city_country is the geocoder's own resolved
    "City, Country" (more reliable than parsing the LLM-written address string), or None
    for city_country if the geocoder didn't return structured address details."""
    # Nominatim's usage policy caps clients at 1 request/second - callers (e.g.
    # research_company looping over several offices per company) can otherwise burst
    # requests fast enough to get the source IP rate-limited/banned across OSM's whole
    # infrastructure, including the tile server used for the admin's map preview.
    time.sleep(1)
    try:
        response = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": address, "format": "json", "limit": 1, "addressdetails": 1, "accept-language": "en"},
            headers={"User-Agent": "MySite-JobApplication/1.0"},
            timeout=10,
        )
        response.raise_for_status()
        results = response.json()
        if results:
            result = results[0]
            addr = result.get("address", {})
            city = addr.get("city") or addr.get("town") or addr.get("village") or addr.get("municipality")
            country = addr.get("country")
            city_country = ', '.join(part for part in (city, country) if part) or None
            return float(result["lat"]), float(result["lon"]), city_country
    except (requests.RequestException, ValueError, KeyError, IndexError) as e:
        print(f"Error geocoding address '{address}': {e}")

    return None


def get_ats_score(cv_summary, job_summary, company_profile='', api_key=None, job_application=None):
    """Returns ATSScoreResult as a dict, or {"error": ...} if the model's answer was unusable."""
    user_prompt = render_prompt(
        'ats_score_user.j2',
        cv_summary=cv_summary,
        job_summary=job_summary,
        company_profile=company_profile.strip() if company_profile else '',
    )
    # ATS_MODEL_FALLBACK is routed by OpenRouter itself on any ATS_MODEL error (LLM_PROFILES).
    response = chat_completion(
        get_client(), purpose='ats_score', job_application=job_application,
        messages=[
            {"role": "system", "content": render_prompt('ats_score_system.j2')},
            {"role": "user", "content": user_prompt},
        ],
        response_format=pydantic_response_format(ATSScoreResult, "ats_score"),
    )
    try:
        return parse_structured(response, ATSScoreResult).model_dump()
    except LLMOutputError as e:
        return {"error": str(e)}


if __name__ == "__main__":
    load_dotenv()

    language = 'de'
    sample = ("Hello! I'm Nikolai, a Python developer with a solid foundation in software engineering and "
              "a keen interest in innovative software development practices.")

    skills = ["Python", "Datenbanken", "APIs"]
    soft_skills = ["teamorientiert"]

    job_title = "Python Developer"
    company_name = "Intercon Solutions GmbH IT & Engineering Experts"
    contact_person = "Vladimir Merdzic"

    cv_intro = get_cover_letter(None, job_title, company_name, sample, language, skills, soft_skills, contact_person)
    print(cv_intro)
