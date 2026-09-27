"""
Health check for the hosting platform.

Render calls `healthCheckPath` (render.yaml) to decide whether an instance is
alive, and restarts one that stops answering. The probe comes from inside the
platform, over plain HTTP and with an internal Host header, so behind the rest
of the middleware it would be redirected to https by SecurityMiddleware or
rejected by ALLOWED_HOSTS in CommonMiddleware — and a healthy site would look
dead. This middleware therefore sits first and answers the one path itself.

The answer says only whether the database is reachable; nothing about the
deployment is exposed.
"""
import logging

from django.db import connection
from django.http import JsonResponse

logger = logging.getLogger(__name__)

HEALTH_PATHS = ('/healthz', '/healthz/')


def health_response():
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
            cursor.fetchone()
    except Exception:
        logger.exception('Health check: database unreachable')
        return JsonResponse({'status': 'error', 'database': 'unreachable'}, status=503)
    return JsonResponse({'status': 'ok', 'database': 'ok'})


class HealthCheckMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path in HEALTH_PATHS and request.method in ('GET', 'HEAD'):
            return health_response()
        return self.get_response(request)
