import json
import os
import re
import time
from typing import List, Literal
from dotenv import load_dotenv
from langdetect import detect
from jinja2 import Environment, FileSystemLoader
from pydantic import BaseModel, ConfigDict, Field
import requests
import newspaper
import openai
from bs4 import BeautifulSoup

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
MODEL = "openai/gpt-5.6-luna"
EXTRACTION_MODEL = "openai/gpt-5-mini"
ATS_MODEL = "anthropic/claude-opus-4.8"
ATS_MODEL_FALLBACK = "openai/gpt-5.2"
COVER_LETTER_MODEL = "anthropic/claude-sonnet-5"
EMPLOYEES_MODEL = "openai/gpt-5-nano"
RESEARCH_MODEL = "google/gemini-2.5-pro:online"
ADDRESS_MODEL = "google/gemini-3-flash-preview:online"

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


class VacancyExtraction(BaseModel):
    model_config = ConfigDict(extra='forbid')

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
            "True if the entity posting this vacancy is a staffing/recruitment agency, IT consultancy, or "
            "outstaffing company hiring on behalf of an external end client, rather than the actual employer "
            "the candidate would work for day-to-day. False if the posting company is the real employer."
        )
    )
    contact_person: str = Field(description="Contact person, unchanged (proper noun); empty if none")
    skills: List[str] = Field(description="Technical skills mentioned in the vacancy, unchanged (original tech terms)")
    soft_skills: List[str] = Field(description="Likely required soft skills, in Russian")
    required_experience: str = Field(description="Required experience, in Russian")
    salary_range: str = Field(description="Salary range if mentioned, in Russian; empty if not mentioned")
    vacation_days: str = Field(description="Vacation days if mentioned, in Russian; empty if not mentioned")
    other_benefits: List[str] = Field(description="Other benefits mentioned in the vacancy, in Russian")
    language: str = Field(description="Actual language of the source vacancy text: 'en' or 'de'")
    german: str = Field(description="Most likely required level of German proficiency, A1 to C2; empty if not applicable")
    minuses: List[str] = Field(description="Potential negatives/red flags of the vacancy, in Russian")
    info: str = Field(description="Short info about the company, in Russian")
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
            "True if the vacancy's work location is in Switzerland — infer this from any Swiss city/canton "
            "name (e.g. Zurich, Zuerich, Winterthur, Bern, Basel, Geneva, Lausanne, Zug, Lucerne, Aargau, "
            "St. Gallen), CHF/Swiss franc salary figures, or an explicit country mention (Switzerland/Schweiz/ "
            "Suisse/Svizzera), even if the word 'Switzerland' itself never appears in the text. False for "
            "fully remote roles with no Swiss location signal, or roles located elsewhere."
        )
    )
    has_growth_signal: bool = Field(
        description=(
            "True if the vacancy text signals company growth/stability: mentions of a funding round or "
            "raised capital (e.g. '$X funding', 'Series A/B'), a growing team, hiring for multiple similar "
            "roles, named well-known clients, or explicit YoY growth figures (e.g. 'N% growth'). "
            "False if no such signal is present."
        )
    )
    has_toxic_flag: bool = Field(
        description=(
            "True if the vacancy text signals a toxic/high-pressure culture, or other red-flag "
            "workplace-culture signals: phrases like 'top-1% performer', 'extreme ownership', '10x engineer', "
            "expectations of long hours/unlimited availability, similar hustle-culture language, notably low "
            "salary explicitly stated for the role's scope, or petty/rigid workplace rules (e.g. mandatory "
            "kitchen-cleaning duty rosters, strict clocking-in requirements). False if the tone is "
            "ordinary/professional."
        )
    )
    has_overregulated_flag: bool = Field(
        description=(
            "True if EITHER: (a) the vacancy text explicitly frames regulation as limiting day-to-day "
            "engineering work, e.g. 'highly regulated industry limits experimentation', 'strict compliance "
            "processes' as a stated constraint; OR (b) the company is in an explicitly regulated industry "
            "(bank, insurance, healthcare, government/public sector) AND the vacancy itself mentions "
            "governance/compliance/audit/risk-management processes as part of the day-to-day work (e.g. "
            "'responsible AI', 'audit trail', 'approval process', 'risk committee sign-off', 'governance "
            "framework'). Job ads never self-admit that bureaucracy slows them down, so criterion (b) is the "
            "realistic signal for most banking/insurance/healthcare/public-sector postings. False only when "
            "neither an explicit limiting statement nor this regulated-industry + governance-language "
            "combination is present — being in a regulated industry with no governance/compliance language at "
            "all is not enough on its own."
        )
    )
    german_blocks_daily_work: bool = Field(
        description=(
            "True if German is described as required for actual day-to-day work (daily stand-ups, "
            "documentation, or communication with colleagues/clients in German), not just a nice-to-have or "
            "a formality. False if English is the working language, or German is only mentioned as a plus."
        )
    )


class ATSScoreResult(BaseModel):
    model_config = ConfigDict(extra='forbid')

    score: int = Field(
        description=(
            "0-10 realistic ATS-style score based on: keyword/semantic match (~45%), experience relevance (~30%), "
            "skills coverage (~15%), CV structure/completeness (~10%). Soft skills barely count — do not let them "
            "inflate the score."
        )
    )
    verdict: Literal["Подходит", "Условно подходит", "Не подходит"]
    hard_filter_risks: List[str] = Field(
        description=(
            "Binary/hard requirements from the vacancy that could cause automatic rejection BEFORE scoring even "
            "happens, regardless of overall fit — e.g. minimum years of experience, mandatory degree, required "
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
    # Tier-scoring signals (see JobApplication.tier) - re-derived here with full context
    # (vacancy text + company research), not just the vacancy text alone as at parse time.
    has_growth_signal: bool = Field(
        description=(
            "True if the vacancy or company research signals company growth/stability: a funding round or "
            "raised capital ('$X funding', 'Series A/B'), a growing team, hiring for multiple similar roles, "
            "named well-known clients, or explicit YoY growth figures ('N% growth'). False if no such signal."
        )
    )
    has_toxic_flag: bool = Field(
        description=(
            "True if the vacancy or company research signals a toxic/high-pressure culture, or other red-flag "
            "workplace-culture signals: phrases like 'top-1% performer', 'extreme ownership', '10x engineer', "
            "expectations of long hours/unlimited availability, similar hustle-culture language, negative "
            "reviews mentioning burnout/overtime, notably low salary or minimal/no raises mentioned in "
            "employee reviews, or petty/rigid workplace-culture red flags (e.g. mandatory kitchen-cleaning "
            "duty rosters, strict clocking-in rules, other pettiness commonly reported in German corporate "
            "culture reviews)."
        )
    )
    has_overregulated_flag: bool = Field(
        description=(
            "True if EITHER: (a) the vacancy or company research explicitly frames regulation as limiting "
            "day-to-day engineering work, e.g. 'highly regulated industry limits experimentation', 'strict "
            "compliance processes' as a stated constraint; OR (b) the company is in an explicitly regulated "
            "industry (bank, insurance, healthcare, government/public sector) AND the vacancy/research mentions "
            "governance/compliance/audit/risk-management processes as part of the day-to-day work (e.g. "
            "'responsible AI', 'audit trail', 'approval process', 'risk committee sign-off', 'governance "
            "framework'). Job ads never self-admit that bureaucracy slows them down, so criterion (b) is the "
            "realistic signal for most banking/insurance/healthcare/public-sector postings. False only when "
            "neither an explicit limiting statement nor this regulated-industry + governance-language "
            "combination is present."
        )
    )
    german_blocks_daily_work: bool = Field(
        description=(
            "True if German is required for actual day-to-day work (daily stand-ups, documentation, or "
            "communication with colleagues/clients in German), not just a nice-to-have or a formality."
        )
    )


class CoverLetterParts(BaseModel):
    model_config = ConfigDict(extra='forbid')

    greeting: str = Field(description="The greeting line only, e.g. 'Dear Mr. Smith,' or 'Sehr geehrte Damen und Herren,'")
    middle_paragraph: str = Field(
        description="Current relevant work matched to this vacancy, plus an honest, positive acknowledgment of one gap"
    )
    closing_paragraph: str = Field(
        description="How the candidate's experience is useful for this specific role and company"
    )


class CompanyEmployees(BaseModel):
    model_config = ConfigDict(extra='forbid')

    employees: str = Field(
        description="Employee count/range exactly as stated in the report (e.g. '22', '11-50', 'Информация не найдена')"
    )


class CompanyOffices(BaseModel):
    model_config = ConfigDict(extra='forbid')

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


def chat_completion(client, purpose, job_application=None, retry_of=None, **kwargs):
    """Wraps client.chat.completions.create() to log the full prompt/response, latency,
    token usage and cost (in USD, as reported by OpenRouter) to LLMCallLog, so both LLM
    spend and output quality can be traced per feature/job application."""
    extra_body = kwargs.pop('extra_body', None) or {}
    extra_body.setdefault('usage', {'include': True})

    start = time.monotonic()
    try:
        response = client.chat.completions.create(extra_body=extra_body, **kwargs)
    except Exception as e:
        latency_ms = int((time.monotonic() - start) * 1000)
        try:
            from mysite.job_application.models import LLMCallLog
            LLMCallLog.objects.create(
                purpose=purpose,
                model=kwargs.get('model', ''),
                messages=kwargs.get('messages'),
                temperature=kwargs.get('temperature'),
                max_tokens=kwargs.get('max_tokens'),
                response_format=kwargs.get('response_format'),
                latency_ms=latency_ms,
                error=str(e),
                job_application=job_application,
                retry_of=retry_of,
            )
        except Exception as log_error:
            print(f"Failed to log failed LLM call for {purpose}: {log_error}")
        raise
    latency_ms = int((time.monotonic() - start) * 1000)

    usage = getattr(response, 'usage', None)
    response_text = None
    if getattr(response, 'choices', None):
        response_text = response.choices[0].message.content

    try:
        from mysite.job_application.models import LLMCallLog
        usage_dict = {}
        if usage is not None:
            usage_dict = usage.model_dump() if hasattr(usage, 'model_dump') else dict(usage)
        LLMCallLog.objects.create(
            purpose=purpose,
            model=kwargs.get('model', ''),
            messages=kwargs.get('messages'),
            response_text=response_text,
            latency_ms=latency_ms,
            temperature=kwargs.get('temperature'),
            max_tokens=kwargs.get('max_tokens'),
            response_format=kwargs.get('response_format'),
            prompt_tokens=usage_dict.get('prompt_tokens'),
            completion_tokens=usage_dict.get('completion_tokens'),
            total_tokens=usage_dict.get('total_tokens'),
            cost=usage_dict.get('cost'),
            job_application=job_application,
            retry_of=retry_of,
        )
    except Exception as e:
        print(f"Failed to log LLM usage for {purpose}: {e}")

    return response


def get_html_content(url):
    try:
        response = requests.get(url)
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
    html = get_html_content(url)
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


def get_openai_response(prompt_text, api_key=None, model_id=None):
    client = get_client()

    response = chat_completion(
        client, purpose='extract_vacancy',
        model=EXTRACTION_MODEL,
        messages=[
            {"role": "system", "content": render_prompt('vacancy_extraction_system.j2')},
            {"role": "user", "content": prompt_text},
        ],
        temperature=0.2,
        max_tokens=6000,
        response_format=pydantic_response_format(VacancyExtraction, "vacancy_extraction"),
    )

    return response.choices[0].message.content if response.choices else None


def get_full_text_ru(prompt_text, api_key=None):
    client = get_client()

    response = chat_completion(
        client, purpose='full_text_translation',
        model=MODEL,
        messages=[
            {"role": "system", "content": render_prompt('full_text_translation_system.j2')},
            {"role": "user", "content": prompt_text},
        ],
        temperature=0.2,
        max_tokens=4096,
    )

    if not response.choices:
        return ''

    return sanitize_llm_text(response.choices[0].message.content)


def process_json(response):
    if not response:
        return {"error": "Empty response from the API"}

    cleaned_response = response.replace("```json", "").replace("```", "").strip()

    try:
        result = json.loads(cleaned_response)
        return result
    except json.JSONDecodeError:
        return {"error": "Invalid response from the API"}


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
    client = get_client()

    skills_formatted = '\n'.join(skills)
    soft_skills_formatted = '\n'.join(soft_skills)
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
        skills_formatted=skills_formatted,
        soft_skills_formatted=soft_skills_formatted,
    )

    response = chat_completion(
        client, purpose='cv_intro', job_application=job_application,
        model=MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.6,
        max_tokens=1024,
    )

    if not response.choices:
        return ''

    text = sanitize_llm_text(response.choices[0].message.content)
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

    skills_formatted = '\n'.join(skills)
    soft_skills_formatted = '\n'.join(soft_skills)
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
        skills_formatted=skills_formatted,
        soft_skills_formatted=soft_skills_formatted,
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
        closing = parts['closing_paragraph'].strip()
        if is_switzerland:
            closing = f"{closing} {SWISS_WORK_PERMIT_NOTE.get(language, SWISS_WORK_PERMIT_NOTE['en'])}"
        return sanitize_llm_text('\n\n'.join([
            parts['greeting'].strip(),
            wurth_intro,
            parts['middle_paragraph'].strip(),
            differentiator,
            closing,
            f"{signoff}\n{sender_name}",
        ]))

    # COVER_LETTER_MODEL occasionally returns an empty completion (transient provider
    # hiccup, or an overly strict content-safety filter tripped by phrasing like "write
    # access to production systems") — retry a few times, then fall back to MODEL.
    for attempt in range(3):
        if attempt > 0:
            time.sleep(1.5 * attempt)
        response = chat_completion(
            client, purpose='cover_letter', job_application=job_application,
            model=COVER_LETTER_MODEL,
            messages=messages,
            temperature=0.8,
            max_tokens=4000,
            response_format=response_format,
        )
        content = response.choices[0].message.content
        if content:
            try:
                parts = json.loads(content)
            except json.JSONDecodeError:
                continue
            if parts.get('greeting') and parts.get('middle_paragraph') and parts.get('closing_paragraph'):
                return assemble(parts)

    response = chat_completion(
        client, purpose='cover_letter_fallback', job_application=job_application,
        model=MODEL,
        messages=messages,
        temperature=0.8,
        max_tokens=1500,
        response_format=response_format,
    )
    content = response.choices[0].message.content
    if not content:
        return ''
    try:
        return assemble(json.loads(content))
    except json.JSONDecodeError:
        return ''


def get_company_research(company_name, location='', api_key=None, job_application=None):
    client = get_client()

    query = f"{company_name}" + (f", {location}" if location else "")

    response = chat_completion(
        client, purpose='company_research', job_application=job_application,
        model=RESEARCH_MODEL,
        messages=[
            {"role": "system", "content": render_prompt('company_research_system.j2')},
            {"role": "user", "content": f"Company to research: {query}"},
        ],
        temperature=0.3,
        max_tokens=4000,
    )

    if not response.choices:
        return 'Информация не найдена'

    return sanitize_llm_text(response.choices[0].message.content) or 'Информация не найдена'


def get_company_reviews(company_name, location='', job_application=None):
    """A single :online web search tends to spend its result budget on general company
    facts and rarely surfaces Kununu/Glassdoor pages, so this is a dedicated call with a
    query that explicitly targets review platforms (same rationale as splitting out
    get_company_addresses/extract_company_employees)."""
    client = get_client()

    query = f"{company_name}" + (f", {location}" if location else "")

    response = chat_completion(
        client, purpose='company_reviews', job_application=job_application,
        model=RESEARCH_MODEL,
        extra_body={'plugins': [{'id': 'web', 'max_results': 8}]},
        messages=[
            {"role": "system", "content": render_prompt('company_reviews_system.j2')},
            {"role": "user", "content": (
                f"Company: {query}. Search for \"{query} kununu\" and \"{query} glassdoor reviews\"."
            )},
        ],
        temperature=0.3,
        max_tokens=2000,
    )

    if not response.choices:
        return 'Информация не найдена'

    return sanitize_llm_text(response.choices[0].message.content) or 'Информация не найдена'


def extract_company_employees(company_research_text, job_application=None):
    client = get_client()

    response = chat_completion(
        client, purpose='company_employees', job_application=job_application,
        model=EMPLOYEES_MODEL,
        messages=[
            {"role": "system", "content": render_prompt('company_employees_system.j2')},
            {"role": "user", "content": company_research_text},
        ],
        temperature=0,
        max_tokens=2000,
        response_format=pydantic_response_format(CompanyEmployees, "company_employees"),
    )

    if not response.choices:
        return ''

    result = process_json(response.choices[0].message.content)
    return result.get('employees', '') if 'error' not in result else ''


def get_company_addresses(company_name, location='', api_key=None, job_application=None):
    """Returns a list of known office addresses (most relevant to the vacancy location
    first), or an empty list if none could be found."""
    client = get_client()

    query = f"{company_name}" + (f", {location}" if location else "")

    response = chat_completion(
        client, purpose='company_address', job_application=job_application,
        model=ADDRESS_MODEL,
        messages=[
            {"role": "system", "content": render_prompt('company_address_system.j2')},
            {"role": "user", "content": f"Company: {query}"},
        ],
        temperature=0.2,
        max_tokens=1500,
        response_format=pydantic_response_format(CompanyOffices, "company_offices"),
    )

    if not response.choices:
        return []

    result = process_json(response.choices[0].message.content)
    if 'error' in result:
        return []

    addresses = []
    for address in result.get('addresses', []):
        address = sanitize_llm_text(address).splitlines()[0].strip()
        # Strip markdown links/citations the model may add despite instructions
        address = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', address).strip(' \t*_')
        if address:
            addresses.append(address)
    return addresses


def get_company_website(company_name, location='', api_key=None, job_application=None):
    client = get_client()

    query = f"{company_name}" + (f", {location}" if location else "")

    response = chat_completion(
        client, purpose='company_website', job_application=job_application,
        model=ADDRESS_MODEL,
        messages=[
            {"role": "system", "content": render_prompt('company_website_system.j2')},
            {"role": "user", "content": f"Company: {query}"},
        ],
        temperature=0.2,
        max_tokens=500,
    )

    if not response.choices:
        return ''

    domain = sanitize_llm_text(response.choices[0].message.content) or ''
    if not domain:
        return ''
    domain = domain.splitlines()[0].strip()
    domain = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', domain).strip(' \t*_')
    domain = re.sub(r'^https?://', '', domain, flags=re.IGNORECASE)
    domain = domain.removeprefix('www.').rstrip('/')

    if not domain or 'не найден' in domain.lower():
        return ''
    return domain


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
    client = get_client()

    user_prompt = render_prompt(
        'ats_score_user.j2',
        cv_summary=cv_summary,
        job_summary=job_summary,
        company_profile=company_profile.strip() if company_profile else '',
    )

    messages = [
        {"role": "system", "content": render_prompt('ats_score_system.j2')},
        {"role": "user", "content": user_prompt},
    ]
    response_format = pydantic_response_format(ATSScoreResult, "ats_score")

    try:
        response = chat_completion(
            client, purpose='ats_score', job_application=job_application,
            model=ATS_MODEL,
            messages=messages,
            temperature=0.2,
            max_tokens=1536,
            response_format=response_format,
        )
    except openai.BadRequestError:
        # ATS_MODEL may not support structured outputs — fall back to a model that does
        response = chat_completion(
            client, purpose='ats_score_fallback', job_application=job_application,
            model=ATS_MODEL_FALLBACK,
            messages=messages,
            temperature=0.2,
            max_tokens=1536,
            response_format=response_format,
        )

    if not response.choices:
        return {"error": "Empty response from the API"}

    return process_json(response.choices[0].message.content)


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
