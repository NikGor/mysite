# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Django 4.1 personal portfolio/CV site. Beyond rendering a public resume page, it exports the CV to PDF (WeasyPrint), supports English/German content, and includes an AI-powered job-application pipeline that parses a vacancy (from a URL or pasted text) and generates a tailored cover letter and CV intro via an LLM.

## Commands

Everything is driven through the `Makefile` (Poetry-based):

- `make install` — `poetry install`
- `make run` — run the dev server (`runserver`)
- `make test` — Django test runner (`manage.py test`); this is what CI runs
- `make lint` — `flake8 mysite` (max line length 140, see `.flake8`)
- `make check` — `manage.py check`
- `make migration` — makemigrations + migrate
- `make dumpdata` / `make loaddata` — snapshot/restore content across all content apps to/from `dumps/` and `content_dump.json`

`mysite/job_application/tests.py` is written as **pytest** functions (not Django `TestCase`) and hits live LLM APIs — run a single test with `poetry run pytest mysite/job_application/tests.py::<name>`. These are not collected by `make test`.

## Configuration

Settings read from a `.env` (via `python-dotenv`). `DATABASE_URL` is parsed by `dj-database-url` (SQLite `db.sqlite3` locally, Postgres in prod). Key vars: `SECRET_KEY`, `DEBUG`, `DATABASE_URL`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`. Deployed on Railway/Render (see `ALLOWED_HOSTS`); static files served via WhiteNoise.

## Architecture

**Custom user is the site.** `AUTH_USER_MODEL = 'user.User'`, and views operate on `get_user_model().objects.first()` — the single first User row *is* the portfolio owner. Content apps relate back to it via reverse accessors (`user.experience_set`, `user.education_set`, `user.skill_set`, etc.), typically ordered by an `order` field.

**Content apps** live under `mysite/` as separate Django apps: `user`, `experience`, `education`, `skills`, `projects`, `open_source`, `job_application`. Each is namespaced `mysite.<app>` in `INSTALLED_APPS`. Editing content is done through Django admin, not custom CRUD views.

**Translation (en/de).** `mysite/translation.py` registers `django-modeltranslation` options per model, which generate `<field>_en` / `<field>_de` DB columns. Code sometimes selects a language field dynamically (e.g. `getattr(user, f'about_me_{language}')`). UI strings use gettext with catalogs in `locale/de/LC_MESSAGES/`. `scripts/utils.py::translate_model` is an admin action that auto-translates all registered translatable fields via the LLM.

**PDF export.** `mysite/views.py` (`ExportPDFView`, `PDFview`) renders `templates/pdf/*.html` to a string and converts with WeasyPrint. PDF routes are wrapped by the `force_language` decorator in `mysite/urls.py` to force rendering locale (`/export2pdf/` en vs `/export2pdf/de/`) regardless of request language.

**Job-application pipeline** (`mysite/job_application/`) — the most involved subsystem, exposed as a DRF API under `/api/` (swagger at `/docs/`):
- `parser.py` — fetches/extracts vacancy text (`requests` + BeautifulSoup + `newspaper3k`/`readability`), detects language (`langdetect`), then calls an LLM to extract structured fields (company, title, skills, language) and to generate the cover letter + CV intro. **LLM calls go through OpenRouter** (`base_url=https://openrouter.ai/api/v1`, `MODEL = "openai/gpt-5.6-luna"`, key `OPENROUTER_API_KEY`) despite the `openai` SDK and lingering `OPENAI_API_KEY` references. German output preserves English tech terms per `DE_ANGLICISMS_RULE`.
- `views.py` — `ParseURLView` / `ParseTextView` (CSRF-exempt) orchestrate parse → extract → generate, seeding the LLM with the owner's `about_me_<lang>` and `cover_letter_sample` as style samples. Note `views.py` calls `django.setup()` at import time.
- `chrome_extension/` — a Manifest V2 browser extension (`popup.js`, `content.js`) that scrapes the current tab and posts to these endpoints.

`samples/` holds style reference files (cover letter, LinkedIn, sample CVs) used to prime generation.
