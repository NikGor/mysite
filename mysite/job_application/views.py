import json
import os
import django
from django.http import JsonResponse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from dotenv import load_dotenv
from drf_yasg.utils import swagger_auto_schema
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from mysite.job_application.models import JobApplication, LLMCallLog
from mysite.job_application.parser import get_job_text, get_openai_response, process_json, get_full_text_ru, \
    get_company_website
from mysite.job_application.serializers import JobApplicationSerializer

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'mysite.settings')
django.setup()


class ParseURLView(APIView):
    """
    Обработчик запросов для парсинга URL.
    """

    @swagger_auto_schema(operation_description="Получить данные по заданному URL", query_serializer=JobApplicationSerializer)
    def get(self, request, *args, **kwargs):
        load_dotenv()
        api_key = os.getenv("OPENAI_API_KEY")

        url = request.GET.get('url', '')
        if not url:
            return Response({'error': 'No URL provided'}, status=status.HTTP_400_BAD_REQUEST)

        parse_started_at = timezone.now()
        prompt_text = get_job_text(url)
        response = get_openai_response(prompt_text, api_key)
        result = process_json(response)

        if 'error' in result:
            return Response(result, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        company_name = result.get('company_name', '')
        job_title = result.get('job_title', '')
        contact_person = result.get('contact_person', '')
        key_skills = ', '.join(result.get('skills', []))
        soft_skills = ', '.join(result.get('soft_skills', []))
        language = result.get('language', '')
        location = ', '.join(result.get('location', []))

        full_text_ru = get_full_text_ru(prompt_text, api_key)
        company_website = get_company_website(company_name, location)

        # Создание нового объекта JobApplication с полученными данными.
        # cv_intro/cover_letter/experience сознательно не генерируются здесь — только
        # извлечение вакансии, чтобы не тратить LLM-вызовы до ручного решения откликаться.
        # Генерация происходит отдельно через admin-действие "Regenerate all AI content".
        job_application = JobApplication(
            company_name=company_name,
            job_title=job_title,
            url=url,
            location=location,
            work_mode=result.get('work_mode', 'office'),
            is_agency=result.get('is_agency', False),
            contact_person=contact_person,
            date_added=timezone.now(),
            key_skills=key_skills,
            soft_skills=soft_skills,
            required_experience=result.get('required_experience', ''),
            salary_range=result.get('salary_range', ''),
            vacation_days=result.get('vacation_days', ''),
            other_benefits='\n'.join(['- ' + benefit for benefit in result.get('other_benefits', [])]),
            minuses='\n'.join(['- ' + minus for minus in result.get('minuses', [])]),
            language=language,
            german_level=result.get('german', ''),
            info=result.get('info', ''),
            full_text_ru=full_text_ru,
            company_website=company_website,
            application_instructions=result.get('application_instructions', ''),
            is_switzerland=result.get('is_switzerland', False),
            has_growth_signal=result.get('has_growth_signal', False),
            has_toxic_flag=result.get('has_toxic_flag', False),
            has_overregulated_flag=result.get('has_overregulated_flag', False),
            german_blocks_daily_work=result.get('german_blocks_daily_work', False),
            # Добавьте другие поля при необходимости
        )
        job_application.save()
        LLMCallLog.objects.filter(
            job_application__isnull=True, created_at__gte=parse_started_at,
        ).update(job_application=job_application)

        # Добавляем статус 'success' к result
        result['status'] = 'success'

        return Response(result, status=status.HTTP_201_CREATED)


@method_decorator(csrf_exempt, name='dispatch')
class ParseTextView(View):
    """
    Обработчик запросов для парсинга текста.
    """

    def post(self, request, *args, **kwargs):
        load_dotenv()
        api_key = os.getenv("OPENAI_API_KEY")

        # Изменяем получение данных: вместо URL из параметров, берем текст из тела запроса
        body_unicode = request.body.decode('utf-8')
        body = json.loads(body_unicode)
        prompt_text = body.get('text', '')

        if not prompt_text:
            return JsonResponse({'error': 'No text provided'}, status=400)

        # Используем тот же метод parse_url
        parse_started_at = timezone.now()
        response = get_openai_response(prompt_text, api_key)
        result = process_json(response)

        if 'error' in result:
            return Response(result, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        company_name = result.get('company_name', '')
        job_title = result.get('job_title', '')
        contact_person = result.get('contact_person', '')
        key_skills = ', '.join(result.get('skills', []))
        soft_skills = ', '.join(result.get('soft_skills', []))
        language = result.get('language', '')
        location = ', '.join(result.get('location', []))

        full_text_ru = get_full_text_ru(prompt_text, api_key)
        company_website = get_company_website(company_name, location)

        # Создание нового объекта JobApplication с полученными данными.
        # cv_intro/cover_letter/experience сознательно не генерируются здесь — только
        # извлечение вакансии, чтобы не тратить LLM-вызовы до ручного решения откликаться.
        # Генерация происходит отдельно через admin-действие "Regenerate all AI content".
        job_application = JobApplication(
            company_name=company_name,
            job_title=job_title,
            location=location,
            work_mode=result.get('work_mode', 'office'),
            is_agency=result.get('is_agency', False),
            contact_person=contact_person,
            date_added=timezone.now(),
            key_skills=key_skills,
            soft_skills=soft_skills,
            required_experience=result.get('required_experience', ''),
            salary_range=result.get('salary_range', ''),
            vacation_days=result.get('vacation_days', ''),
            other_benefits='\n'.join(['- ' + benefit for benefit in result.get('other_benefits', [])]),
            minuses='\n'.join(['- ' + minus for minus in result.get('minuses', [])]),
            language=language,
            german_level=result.get('german', ''),
            info=result.get('info', ''),
            full_text_ru=full_text_ru,
            company_website=company_website,
            application_instructions=result.get('application_instructions', ''),
            is_switzerland=result.get('is_switzerland', False),
            has_growth_signal=result.get('has_growth_signal', False),
            has_toxic_flag=result.get('has_toxic_flag', False),
            has_overregulated_flag=result.get('has_overregulated_flag', False),
            german_blocks_daily_work=result.get('german_blocks_daily_work', False),
            # Добавьте другие поля при необходимости
        )
        job_application.save()
        LLMCallLog.objects.filter(
            job_application__isnull=True, created_at__gte=parse_started_at,
        ).update(job_application=job_application)

        result['status'] = 'success'
        return JsonResponse(result, status=201)
