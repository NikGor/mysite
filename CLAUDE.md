# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Django 4.1 personal portfolio/CV site. Beyond rendering a public resume page, it exports the CV to PDF (WeasyPrint), supports English/German content, and includes an AI-powered job-application pipeline that parses a vacancy (from a URL or pasted text) and generates a tailored cover letter and CV intro via an LLM.

## Commands

Everything is driven through the `Makefile` (Poetry-based) — see it for the exact targets (install/run/test/lint/check/migration/dumpdata/loaddata); `make lint`/`make test` mirror what `.github/workflows/python-app.yml` runs in CI.

`mysite/job_application/tests.py` is written as **pytest** functions (not Django `TestCase`) and hits live LLM APIs — run a single test with `poetry run pytest mysite/job_application/tests.py::<name>`. These are not collected by `make test`.

## Configuration

Settings read from a `.env` (via `python-dotenv`). `DATABASE_URL` is parsed by `dj-database-url` (SQLite `db.sqlite3` locally, Postgres in prod). Key vars: `SECRET_KEY`, `DEBUG`, `DATABASE_URL`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`. Deployed on Railway/Render (see `ALLOWED_HOSTS`); static files served via WhiteNoise.

## Architecture

**Custom user is the site.** `AUTH_USER_MODEL = 'user.User'`, and views operate on `get_user_model().objects.first()` — the single first User row *is* the portfolio owner. Content apps relate back to it via reverse accessors (`user.experience_set`, `user.education_set`, `user.skill_set`, etc.), typically ordered by an `order` field.

**Content apps** live under `mysite/` as separate Django apps: `user`, `experience`, `education`, `skills`, `projects`, `open_source`, `job_application`. Each is namespaced `mysite.<app>` in `INSTALLED_APPS`. Editing content is done through Django admin, not custom CRUD views.

**Translation (en/de).** `mysite/translation.py` registers `django-modeltranslation` options per model, which generate `<field>_en` / `<field>_de` DB columns. Code sometimes selects a language field dynamically (e.g. `getattr(user, f'about_me_{language}')`). UI strings use gettext with catalogs in `locale/de/LC_MESSAGES/`. `scripts/utils.py::translate_model` is an admin action that auto-translates all registered translatable fields via the LLM.

**PDF export.** `mysite/views.py` (`ExportPDFView`, `PDFview`) renders `templates/pdf/*.html` to a string and converts with WeasyPrint. PDF routes are wrapped by the `force_language` decorator in `mysite/urls.py` to force rendering locale (`/export2pdf/` en vs `/export2pdf/de/`) regardless of request language.

**Job-application pipeline** (`mysite/job_application/`) — the most involved subsystem, exposed as a DRF API under `/api/` (swagger at `/docs/`):
It is a fixed DAG of single LLM calls, not an agent loop — keep it that way unless a step genuinely needs tool-choice.
- `parser.py` — one function per LLM call (fetch page text, extract `VacancyExtraction`, RU translation, company research/reviews/facts, ATS score, CV intro, cover letter) plus `chat_completion()`, which logs every call (prompt, response, tokens, cost, latency) to `LLMCallLog`. **LLM calls go through OpenRouter** (key `OPENROUTER_API_KEY`) despite the `openai` SDK and lingering `OPENAI_API_KEY` references. Models and params are set per purpose in `LLM_PROFILES` (default `openai/gpt-6-luna`, `:online` for web search, Sonnet for cover letters; `fallbacks` use OpenRouter's native `models` routing) — change models there, not at call sites. Structured outputs are Pydantic models validated with `parse_structured()`; the four tier flags live in one shared `TierSignals` model. Prompts are Jinja templates in `prompts/`. German output preserves English tech terms per `_de_anglicisms_rule.j2`.
- `services.py` — the pipeline steps used by views, admin actions and the Gmail import: `ingest_vacancy()` (optional TypeSafe Jev relevance gate on raw text → extraction ∥ translation → `JobApplication`) and `research_job_application()` (research ∥ reviews ∥ facts in threads → employees → geocode → ATS). Company research is reused from another vacancy of the same company for 30 days (admin has a force-refresh action). LLM calls are attached to their `JobApplication` via `track_llm_calls()` (contextvar, propagated into worker threads by `run_parallel()`).
- `views.py` — `ParseURLView` / `ParseTextView` (CSRF-exempt) call `ingest_vacancy()` without the relevance gate. CV intro / cover letter are generated later by the "Regenerate all AI content" admin action, seeded with the owner's `about_me_<lang>` and `cover_letter_sample`.
- `management/commands/import_gmail_vacancies.py` — daily cron on the Pi (LinkedIn job-alert emails → ingest → research), with `--limit` and a per-run `--max-cost-usd` budget.
- `test_pipeline.py` — offline Django tests with a fake LLM client (run by `make test`); `tests.py` is live-API pytest.
- `chrome_extension/` — a Manifest V2 browser extension (`popup.js`, `content.js`) that scrapes the current tab and posts to these endpoints.

`samples/` holds style reference files (cover letter, LinkedIn, sample CVs) used to prime generation.
