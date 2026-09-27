"""
Operations: the health check the host polls, the token-usage record, and the
staff page that reads it.
"""

import asyncio
import datetime
import sys
import types
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.db.utils import OperationalError
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from generator import ai_client
from generator.models import AIUsageDay, Generation


class HealthCheckTest(TestCase):

    def test_healthy(self):
        resp = Client().get('/healthz')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {'status': 'ok', 'database': 'ok'})

    def test_answers_the_platform_probe_whatever_its_host_header(self):
        """Render probes with an internal Host; ALLOWED_HOSTS would reject it anywhere else."""
        c = Client(HTTP_HOST='10.201.3.7:10000')
        self.assertEqual(c.get('/healthz').status_code, 200)
        self.assertEqual(c.get('/').status_code, 400, 'every other path still enforces ALLOWED_HOSTS')

    def test_database_down_is_a_503(self):
        with patch('aigen.middleware.connection.cursor', side_effect=OperationalError('down')):
            resp = Client().get('/healthz')
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()['database'], 'unreachable')

    def test_only_reads(self):
        self.assertEqual(Client().post('/healthz').status_code, 404)


def _fake_anthropic(input_tokens, output_tokens):
    mod = types.ModuleType('anthropic')

    class _Err(Exception):
        pass

    for name in ('RateLimitError', 'APIConnectionError', 'APIStatusError', 'BadRequestError'):
        setattr(mod, name, type(name, (_Err,), {}))

    async def create(**kw):
        return types.SimpleNamespace(
            stop_reason='end_turn', stop_details=None,
            usage=types.SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
            content=[types.SimpleNamespace(type='text', text='ok')])

    mod.AsyncAnthropic = lambda **kw: types.SimpleNamespace(messages=types.SimpleNamespace(create=create))
    return mod


class UsageRecordTest(TestCase):

    def setUp(self):
        cache.clear()

    async def test_each_call_adds_to_todays_row_for_its_model(self):
        with patch.dict(sys.modules, {'anthropic': _fake_anthropic(1200, 300)}):
            for _ in range(3):
                await ai_client.call_anthropic('s', 'u', api_key='k', model='claude-sonnet-5')
        row = await AIUsageDay.objects.aget(date=timezone.now().date(), model='claude-sonnet-5')
        self.assertEqual((row.calls, row.input_tokens, row.output_tokens), (3, 3600, 900))

    async def test_a_failing_usage_write_does_not_lose_the_generation(self):
        with patch.dict(sys.modules, {'anthropic': _fake_anthropic(10, 10)}), \
                patch.object(AIUsageDay.objects, 'filter', side_effect=OperationalError('locked')):
            text = await ai_client.call_anthropic('s', 'u', api_key='k', model='claude-sonnet-5')
        self.assertEqual(text, 'ok')


@override_settings(AI_PRICES_PER_MTOK={'claude-sonnet-5': (2.0, 10.0)}, AI_DAILY_TOKEN_BUDGET=1_000_000)
class StaffUsagePageTest(TestCase):

    def setUp(self):
        cache.clear()
        today = timezone.now().date()
        AIUsageDay.objects.create(date=today, model='claude-sonnet-5', calls=10,
                                  input_tokens=1_000_000, output_tokens=100_000)   # $2 + $1
        AIUsageDay.objects.create(date=today - datetime.timedelta(days=45), model='claude-sonnet-5',
                                  calls=99, input_tokens=9_000_000, output_tokens=0)   # outside 30 days
        AIUsageDay.objects.create(date=today, model='claude-mystery-9', calls=1, input_tokens=5, output_tokens=5)
        self.staff = User.objects.create_user('ops', password='pw-12345', is_staff=True)
        self.member = User.objects.create_user('member', password='pw-12345')
        Generation.objects.create(user=self.member, resume_text='x', job_description='x', result='x')

    def test_members_cannot_see_it(self):
        c = Client()
        c.force_login(self.member)
        resp = c.get(reverse('staff_ai_usage'))
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login/', resp['Location'])

    def test_staff_see_cost_budget_and_heaviest_accounts(self):
        c = Client()
        c.force_login(self.staff)
        resp = c.get(reverse('staff_ai_usage'))
        self.assertEqual(resp.status_code, 200)
        self.assertAlmostEqual(resp.context['today_cost'], 3.0)
        self.assertAlmostEqual(resp.context['period_cost'], 3.0, msg='rows older than 30 days are excluded')
        self.assertEqual(resp.context['budget_pct'], 110)
        self.assertEqual(resp.context['unpriced'], ['claude-mystery-9'])
        self.assertContains(resp, '$3.00')
        self.assertContains(resp, 'member')

    def test_the_menu_links_it_for_staff_only(self):
        c = Client()
        c.force_login(self.staff)
        self.assertContains(c.get(reverse('dashboard')), reverse('staff_ai_usage'))
        c.force_login(self.member)
        self.assertNotContains(c.get(reverse('dashboard')), reverse('staff_ai_usage'))
