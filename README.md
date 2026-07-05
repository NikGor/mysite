![Python application](https://github.com/NikGor/MySite/actions/workflows/python-app.yml/badge.svg)

# Job Application Tracker & AI Cover Letter Pipeline

A Django app for tracking job applications end-to-end: paste a vacancy (via URL or a
browser extension), let an LLM extract structured data (company, role, skills,
salary, application instructions, ATS fit) and generate a tailored cover letter and CV
intro, then track the application through the hiring pipeline (sent, response,
interviews, outcome) in the Django admin.

It also renders a personal CV/portfolio page and exports it to PDF, in English and
German, since the tracked applications and generated documents are built on top of
that same CV data.

## What it does

- **Parse a vacancy** from a URL or pasted text (`mysite/job_application/`) — fetches
  and extracts the job text, then uses an LLM (via OpenRouter) to pull out company
  name, role, required skills, salary range, language, remote/agency signals, and
  special application instructions (e.g. "no cover letter", "apply only via portal").
- **Chrome extension** (`chrome_extension/`) scrapes the vacancy page you're viewing
  and sends it straight to the parser API.
- **Generate tailored documents on demand** — a cover letter (fixed structure + LLM-
  written variable parts) and a CV intro, seeded with your own writing style and
  matched to the vacancy's language. Generation is a separate admin action, not run
  automatically at parse time, so browsing vacancies doesn't burn LLM calls.
- **Company research & ATS scoring** — web-search-backed company research (culture,
  reviews, AI focus) and an ATS-style score/verdict comparing your CV against the
  vacancy.
- **Track the pipeline** in Django admin: application sent, response received,
  interview dates, closed/not-interested, with filters and bulk actions
  (regenerate content, research company, export cover letter/CV as TXT/MD/PDF).
- **CV/portfolio page + PDF export** — the underlying personal site, bilingual
  (en/de) via django-modeltranslation, exported with WeasyPrint.

## Architecture

- `mysite/job_application/` — the core pipeline: `parser.py` (LLM calls, structured
  extraction, cover letter/CV intro generation), `views.py` (parse endpoints, DRF API
  under `/api/`, swagger at `/docs/`), `admin.py` (pipeline tracking + bulk actions),
  `prompts/*.j2` (Jinja2 prompt templates).
- `mysite/user`, `experience`, `education`, `skills`, `projects`, `open_source` —
  content apps backing the CV; a single `User` row is the portfolio owner.
- `chrome_extension/` — Manifest V2 extension that scrapes a vacancy page and posts it
  to the parsing API.
- `templates/pdf/` — WeasyPrint templates for CV and cover letter PDF export.

See `CLAUDE.md` for a more detailed architecture walkthrough.

## Local Setup 🛠️

This project uses Poetry and a `Makefile`.

```bash
git clone https://github.com/NikGor/MySite.git
cd MySite
make install        # poetry install
```

Copy `.env.example` (if present) or create a `.env` with at least:

```
SECRET_KEY=...
DEBUG=True
DATABASE_URL=sqlite:///db.sqlite3
OPENROUTER_API_KEY=...
```

Apply migrations and run the dev server:

```bash
make migration       # makemigrations + migrate
make run             # runserver
```

Now navigate to http://localhost:8000 for the site, or
http://localhost:8000/admin/ for the job-application tracker.

## Other commands

```bash
make test    # Django test runner (CI)
make lint    # flake8 mysite
make check   # manage.py check
```

`mysite/job_application/tests.py` uses pytest and hits live LLM APIs; it's not part of
`make test`:

```bash
poetry run pytest mysite/job_application/tests.py::<test_name>
```

## Contributing 🤝

Contributions, issues, and feature requests are welcome! Feel free to check the
issues page.

## License 📝

This project is MIT licensed.
