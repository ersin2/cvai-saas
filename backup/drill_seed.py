"""Seed the throwaway CI database for the backup drill (never run against production)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'aigen.settings')

import django  # noqa: E402

django.setup()

from django.contrib.auth.models import User  # noqa: E402
from django.db import connection  # noqa: E402

from generator.models import AIResult, AIUsageDay, Generation, JobApplication  # noqa: E402

if 'localhost' not in connection.settings_dict.get('HOST', '') and connection.vendor == 'postgresql':
    sys.exit('Refusing to seed a non-local database')

for i in range(5):
    user = User.objects.create_user(f'drill{i}', email=f'drill{i}@example.com', password='x-drill-pass-1')
    Generation.objects.create(user=user, resume_text='Résumé — ünïcode ✓', job_description='JD',
                              company_name='Northwind', result='{"full_name": "Drill"}')
    JobApplication.objects.create(user=user, company_name='Acme', job_title='Engineer', status='applied')
    AIResult.objects.create(user=user, result_type='ats', result='ATS 70/100', score=70)
AIUsageDay.objects.create(date='2026-09-01', model='claude-sonnet-5', calls=3, input_tokens=100, output_tokens=50)
print('seeded', User.objects.count(), 'users')
