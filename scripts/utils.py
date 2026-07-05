import json
import os
import requests
from django.core.serializers.json import DjangoJSONEncoder
from django.http import HttpResponse
from django.template.loader import render_to_string
from django.utils.translation import override
from openai import OpenAI
from modeltranslation.translator import translator
from django.conf import settings
from django.db.models.fields import CharField, TextField
from weasyprint import HTML
from django.contrib.auth import get_user_model

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
MODEL = "openai/gpt-4.1"


def get_client():
    return OpenAI(
        api_key=os.getenv("OPENROUTER_API_KEY"),
        base_url=OPENROUTER_BASE_URL,
    )


def translate_model(modeladmin, request, queryset):
    client = get_client()
    for obj in queryset:
        translation_opts = translator.get_options_for_model(obj.__class__)

        for field_name in translation_opts.fields.keys():
            field_object = obj._meta.get_field(field_name)

            if isinstance(field_object, (CharField, TextField)):
                source_value = getattr(obj, field_name, None)
                if source_value:
                    for lang_code, lang_name in settings.LANGUAGES:
                        if lang_code == settings.LANGUAGE_CODE:
                            continue

                        response = client.chat.completions.create(
                            model=MODEL,
                            messages=[
                                {
                                    "role": "system",
                                    "content": (
                                        "You are a professional CV translator. "
                                        "Translate the given text from English to German. "
                                        "Rules: "
                                        "1. Keep all IT/tech terms and anglicisms as-is (e.g. Python, Django, REST API, backend, frontend, Git, Docker, CI/CD, etc.). "
                                        "2. Output ONLY the translated text — no comments, no explanations, no quotes, no markdown. "
                                        "3. Preserve the original formatting, line breaks and punctuation."
                                    ),
                                },
                                {
                                    "role": "user",
                                    "content": source_value,
                                },
                            ],
                            temperature=0.3,
                            max_tokens=1500,
                        )
                        translated_text = response.choices[0].message.content.strip()
                        translated_field_name = f'{field_name}_{lang_code}'
                        setattr(obj, translated_field_name, translated_text)

        obj.save()


def create_screenshot(modeladmin, request, queryset):
    screenshot_folder = 'static/screenshots'

    # Создать папку screenshots, если она не существует
    if not os.path.exists(screenshot_folder):
        os.makedirs(screenshot_folder)

    for project in queryset:
        url_to_capture = project.url if project.url else project.github_url

        if url_to_capture:
            api_url = "https://api.apiflash.com/v1/urltoimage"
            params = {
                "access_key": "d1753a1756d346b690a66246fee2d66c",
                "url": url_to_capture,
                "accept-language": "en"
            }
            response = requests.get(api_url, params=params)
            if response.status_code == 200:
                screenshot_filename = f"{project.id}_{project.name}.png"
                screenshot_path = os.path.join(screenshot_folder, screenshot_filename)
                with open(screenshot_path, "wb") as f:
                    f.write(response.content)
                project.screenshot_url = f'/screenshots/{screenshot_filename}'
                project.save()


def export_cover_letter(modeladmin, request, queryset):
    job_application = queryset.first()
    response = HttpResponse(job_application.cover_letter, content_type='text/plain')
    file_name = f"{os.getenv('MY_NAME')}_{job_application.company_name}_cover_letter.txt"
    response['Content-Disposition'] = f'attachment; filename="{file_name}"'
    return response


def export_cv(modeladmin, request, queryset):
    for job_application in queryset:
        # В job_application.language хранится код языка, например 'en' или 'de'
        language = job_application.language or 'en'

        with override(language):
            user = get_user_model().objects.first()

            # Подменяем описания опыта на индивидуально сгенерированные под вакансию
            experiences = list(user.experience_set.all().order_by('order'))
            custom_descriptions = {
                je.experience_id: je.description
                for je in job_application.custom_experiences.all()
                if je.description
            }
            for experience in experiences:
                if experience.id in custom_descriptions:
                    setattr(experience, f'description_{language}', custom_descriptions[experience.id])

            educations = user.education_set.all().order_by('order')
            skills = user.skill_set.all()
            context = {
                'user': user,
                'experiences': experiences,
                'educations': educations,
                'skills': skills,
                'about_me': job_application.cv_intro,
            }

            # Используем единый шаблон базового резюме, чтобы адаптированное CV
            # всегда совпадало с ним по вёрстке
            html_string = render_to_string('pdf/pdf_template.html', context)
            pdf = HTML(string=html_string).write_pdf()

            response = HttpResponse(pdf, content_type='application/pdf')
            file_name = f"{os.getenv('MY_NAME')}_{job_application.company_name}_cv.pdf"
            response['Content-Disposition'] = f'attachment; filename="{file_name}"'

        return response


def export_json(modeladmin, request, queryset):
    data = [
        {field.name: getattr(job_application, field.name) for field in job_application._meta.fields}
        for job_application in queryset
    ]

    payload = json.dumps(data, indent=2, ensure_ascii=False, cls=DjangoJSONEncoder)
    response = HttpResponse(payload, content_type='application/json')
    response['Content-Disposition'] = 'attachment; filename="job_applications_export.json"'
    return response

export_json.short_description = "Export selected to JSON (all fields)"
