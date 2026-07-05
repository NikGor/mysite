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
MODEL = "openai/gpt-4.1"
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
    is_remote: bool = Field(description="Whether remote work is possible")
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

    response = client.chat.completions.create(
        model=EXTRACTION_MODEL,
        messages=[
            {"role": "system", "content": render_prompt('vacancy_extraction_system.j2')},
            {"role": "user", "content": prompt_text},
        ],
        temperature=0.2,
        max_tokens=6000,
        response_format=pydantic_response_format(VacancyExtraction, "vacancy_extraction"),
    )

    return response.choices[0].message.content


def get_full_text_ru(prompt_text, api_key=None):
    client = get_client()

    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": render_prompt('full_text_translation_system.j2')},
            {"role": "user", "content": prompt_text},
        ],
        temperature=0.2,
        max_tokens=4096,
    )

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


def get_cv_intro(api_key, sample, language, skills, soft_skills, is_switzerland=False):
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

    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.6,
        max_tokens=1024,
    )

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
                      is_switzerland=False):
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
        response = client.chat.completions.create(
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

    response = client.chat.completions.create(
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


def get_experience_description(original_description, job_title, company_name, language, key_skills, soft_skills):
    client = get_client()
    original_lines = original_description.count('\n') + 1
    max_chars = len(original_description)

    system_prompt = render_prompt(
        'experience_description_system.j2',
        max_chars=max_chars,
        original_length=len(original_description),
        original_lines=original_lines,
        job_title=job_title,
        company_name=company_name,
        language=language,
        language_name=LANGUAGE_NAMES.get(language, 'english'),
    )
    user_prompt = render_prompt(
        'experience_description_user.j2',
        original_description=original_description,
        key_skills=key_skills,
        soft_skills=soft_skills,
    )

    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.7,
        max_tokens=1024,
    )

    return sanitize_llm_text(response.choices[0].message.content)


def get_company_research(company_name, location='', api_key=None):
    client = get_client()

    query = f"{company_name}" + (f", {location}" if location else "")

    response = client.chat.completions.create(
        model=RESEARCH_MODEL,
        messages=[
            {"role": "system", "content": render_prompt('company_research_system.j2')},
            {"role": "user", "content": f"Company to research: {query}"},
        ],
        temperature=0.3,
        max_tokens=4000,
    )

    return sanitize_llm_text(response.choices[0].message.content) or 'Информация не найдена'


def extract_company_employees(company_research_text):
    client = get_client()

    response = client.chat.completions.create(
        model=EMPLOYEES_MODEL,
        messages=[
            {"role": "system", "content": render_prompt('company_employees_system.j2')},
            {"role": "user", "content": company_research_text},
        ],
        temperature=0,
        max_tokens=600,
        response_format=pydantic_response_format(CompanyEmployees, "company_employees"),
    )

    result = process_json(response.choices[0].message.content)
    return result.get('employees', '') if 'error' not in result else ''


def get_company_address(company_name, location='', api_key=None):
    client = get_client()

    query = f"{company_name}" + (f", {location}" if location else "")

    response = client.chat.completions.create(
        model=ADDRESS_MODEL,
        messages=[
            {"role": "system", "content": render_prompt('company_address_system.j2')},
            {"role": "user", "content": f"Company: {query}"},
        ],
        temperature=0.2,
        max_tokens=1500,
    )

    address = sanitize_llm_text(response.choices[0].message.content) or 'Адрес не найден'
    address = address.splitlines()[0].strip()
    # Strip markdown links/citations the model may add despite instructions, e.g. "... [source](url)"
    address = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', address).strip(' \t*_')
    return address


def get_company_website(company_name, location='', api_key=None):
    client = get_client()

    query = f"{company_name}" + (f", {location}" if location else "")

    response = client.chat.completions.create(
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
    try:
        response = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": address, "format": "json", "limit": 1},
            headers={"User-Agent": "MySite-JobApplication/1.0"},
            timeout=10,
        )
        response.raise_for_status()
        results = response.json()
        if results:
            return float(results[0]["lat"]), float(results[0]["lon"])
    except (requests.RequestException, ValueError, KeyError, IndexError) as e:
        print(f"Error geocoding address '{address}': {e}")

    return None


def get_ats_score(cv_summary, job_summary, company_profile='', api_key=None):
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
        response = client.chat.completions.create(
            model=ATS_MODEL,
            messages=messages,
            temperature=0.2,
            max_tokens=1536,
            response_format=response_format,
        )
    except openai.BadRequestError:
        # ATS_MODEL may not support structured outputs — fall back to a model that does
        response = client.chat.completions.create(
            model=ATS_MODEL_FALLBACK,
            messages=messages,
            temperature=0.2,
            max_tokens=1536,
            response_format=response_format,
        )

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
