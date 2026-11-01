import json
from django.http import JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from drf_yasg.utils import swagger_auto_schema
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from mysite.job_application.parser import get_job_text
from mysite.job_application.serializers import JobApplicationSerializer
from mysite.job_application.services import VacancyIngestError, ingest_vacancy


def _ingest(text, url=None):
    """Returns (payload, http_status). No relevance gate here - a manual parse request
    means the user already decided the vacancy is worth tracking."""
    try:
        result = ingest_vacancy(text, url=url)
    except VacancyIngestError as e:
        return {'error': str(e)}, status.HTTP_502_BAD_GATEWAY
    job_application = result.job_application
    payload = {
        'status': 'success',
        'id': job_application.pk,
        'company_name': job_application.company_name,
        'job_title': job_application.job_title,
        'location': job_application.location,
        'language': job_application.language,
        'key_skills': job_application.key_skills,
    }
    return payload, status.HTTP_201_CREATED


class ParseURLView(APIView):
    """
    Обработчик запросов для парсинга URL.
    """

    @swagger_auto_schema(operation_description="Получить данные по заданному URL", query_serializer=JobApplicationSerializer)
    def get(self, request, *args, **kwargs):
        url = request.GET.get('url', '')
        if not url:
            return Response({'error': 'No URL provided'}, status=status.HTTP_400_BAD_REQUEST)

        text = get_job_text(url)
        if not text:
            return Response({'error': f'Could not fetch {url}'}, status=status.HTTP_502_BAD_GATEWAY)

        payload, http_status = _ingest(text, url=url)
        return Response(payload, status=http_status)


@method_decorator(csrf_exempt, name='dispatch')
class ParseTextView(View):
    """
    Обработчик запросов для парсинга текста.
    """

    def post(self, request, *args, **kwargs):
        try:
            body = json.loads(request.body.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return JsonResponse({'error': 'Body must be JSON: {"text": "..."}'}, status=400)
        text = body.get('text', '')
        if not text:
            return JsonResponse({'error': 'No text provided'}, status=400)

        # The extension sends the rendered page text plus its URL, so the server never
        # has to fetch the page itself (LinkedIn rate-limits the Pi with 429).
        url = body.get('url') or None
        payload, http_status = _ingest(text, url=url)
        return JsonResponse(payload, status=http_status)
