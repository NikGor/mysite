import pytest
from dotenv import load_dotenv

from mysite.job_application.parser import (
    get_openai_response,
    process_json,
    get_cv_intro,
    get_cover_letter,
)

load_dotenv()

JOB_TEXT_DE = """
Adesso SE - AI & Automation Developer

Umsetzung von KI-Lösungen von Prototyp bis produktivem Deployment.
Aufbau von MCP-Servern und RAG-Pipelines (Chunking, Embeddings, Vektordatenbanken).
Integration externer KI-Plattformen (Azure OpenAI, Google Vertex AI, Anthropic).

Was wir suchen:
Entwicklungserfahrung in KI/ML (Python und/oder JavaScript/TypeScript).
Sehr gutes Verständnis von LLMs, GenAI, RAG, Embeddings, MCP.
Erfahrung mit LangChain, LlamaIndex, Semantic Kernel.
Sehr gute Deutsch- und Englischkenntnisse.
"""

JOB_TEXT_EN = """
TechCorp - Senior Python Developer

We are looking for a Python developer with experience in Django, REST APIs, Docker.
Remote work possible. Salary: 80k-100k EUR. 30 vacation days.
Contact: John Smith.
"""

CV_SAMPLE_DE = (
    "Hallo! Ich bin Nikolai, ein Python-Entwickler mit solider Grundlage in der Softwareentwicklung "
    "und starkem Interesse an innovativen Technologien."
)

COVER_LETTER_SAMPLE_DE = (
    "Sehr geehrte Damen und Herren, ich bewerbe mich hiermit auf die ausgeschriebene Stelle. "
    "Mit meiner Erfahrung in der Softwareentwicklung bin ich überzeugt, einen wertvollen Beitrag zu leisten."
)


class TestGetOpenaiResponse:
    def test_returns_string(self):
        result = get_openai_response(JOB_TEXT_DE)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_returns_valid_json(self):
        result = get_openai_response(JOB_TEXT_DE)
        parsed = process_json(result)
        assert "error" not in parsed

    def test_parses_german_job(self):
        result = get_openai_response(JOB_TEXT_DE)
        parsed = process_json(result)
        assert parsed.get("language") == "de"
        assert "adesso" in parsed.get("company_name", "").lower()

    def test_parses_english_job(self):
        result = get_openai_response(JOB_TEXT_EN)
        parsed = process_json(result)
        assert parsed.get("language") == "en"
        assert parsed.get("contact_person") != ""

    def test_extracts_skills(self):
        result = get_openai_response(JOB_TEXT_EN)
        parsed = process_json(result)
        skills = parsed.get("skills", [])
        assert isinstance(skills, list)
        assert any("Python" in s for s in skills)

    def test_extracts_salary(self):
        result = get_openai_response(JOB_TEXT_EN)
        parsed = process_json(result)
        assert "80k" in parsed.get("salary_range", "") or "100k" in parsed.get("salary_range", "")


class TestProcessJson:
    def test_valid_json(self):
        raw = '{"job_title": "Developer", "company_name": "ACME"}'
        result = process_json(raw)
        assert result["job_title"] == "Developer"

    def test_strips_markdown_fences(self):
        raw = '```json\n{"job_title": "Developer"}\n```'
        result = process_json(raw)
        assert result["job_title"] == "Developer"

    def test_invalid_json_returns_error(self):
        result = process_json("not json at all")
        assert "error" in result


class TestGetCvIntro:
    def test_returns_string(self):
        result = get_cv_intro(None, CV_SAMPLE_DE, "de", "Python, LangChain, RAG", "teamorientiert")
        assert isinstance(result, str)
        assert len(result) > 50

    def test_contains_keywords(self):
        result = get_cv_intro(None, CV_SAMPLE_DE, "de", "Python, LangChain", "teamorientiert")
        assert "Python" in result or "LangChain" in result

    def test_english_language(self):
        sample_en = "Hi, I'm Nikolai, a Python developer interested in AI and automation."
        result = get_cv_intro(None, sample_en, "en", "Python, RAG, LLMs", "communication")
        assert isinstance(result, str)
        assert len(result) > 50


class TestGetCoverLetter:
    def test_returns_string(self):
        result = get_cover_letter(
            None, "AI Developer", "Adesso SE",
            COVER_LETTER_SAMPLE_DE, "de",
            "Python, LangChain, RAG", "teamorientiert", ""
        )
        assert isinstance(result, str)
        assert len(result) > 50

    def test_neutral_greeting_without_contact(self):
        result = get_cover_letter(
            None, "Python Developer", "TestCorp",
            COVER_LETTER_SAMPLE_DE, "de",
            "Python, Django", "teamorientiert", ""
        )
        assert isinstance(result, str)

    def test_personal_greeting_with_contact(self):
        result = get_cover_letter(
            None, "Python Developer", "TestCorp",
            COVER_LETTER_SAMPLE_DE, "de",
            "Python, Django", "teamorientiert", "Müller"
        )
        assert "Müller" in result

    def test_english_cover_letter(self):
        sample_en = "Dear Hiring Team, I am applying for this position."
        result = get_cover_letter(
            None, "Python Developer", "TechCorp",
            sample_en, "en",
            "Python, Django, REST API", "teamwork", "John Smith"
        )
        assert "Smith" in result
