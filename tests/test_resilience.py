"""
Behaviour under slow, failing or hostile conditions: a job page that never
finishes, an AI provider that is down, password guessing, sign-up floods, and
Stripe being unreachable when someone deletes their account.

Nothing here touches the network. Job pages come from httpx.MockTransport,
the provider is a fake `anthropic` module, and Stripe calls are patched.
"""

import asyncio
import sys
import types
from unittest.mock import patch

import httpx
import stripe
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse

from generator import ai_client
from generator.guards import _url_points_to_public_host, client_ip
from users import views as user_views
from users.models import Profile


class _CleanCache(TestCase):
    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()


# ---------------------------------------------------------------------------
# Job URL scraper
# ---------------------------------------------------------------------------

_REAL_ASYNC_CLIENT = httpx.AsyncClient
_REAL_HOST_CHECK = _url_points_to_public_host


def _serve(handler):
    """Route the scraper's AsyncClient through `handler` instead of the network."""
    def factory(*args, **kwargs):
        kwargs['transport'] = httpx.MockTransport(handler)
        return _REAL_ASYNC_CLIENT(*args, **kwargs)
    return patch('generator.views.httpx.AsyncClient', factory)


def _jobs_host_is_public():
    """jobs.example is 'public'; everything else goes through the real check."""
    return patch(
        'generator.views._url_points_to_public_host',
        side_effect=lambda url: (True, None) if '//jobs.example' in url else _REAL_HOST_CHECK(url),
    )


class ScrapeJobUrlTest(_CleanCache):

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user('scraper', password='pw-12345')
        Profile.objects.filter(user=self.user).update(plan='elite')
        self.client = Client()
        self.client.force_login(self.user)

    def scrape(self, url='https://jobs.example/posting'):
        return self.client.post(reverse('scrape_job'), {'url': url})

    def test_a_normal_page_is_reduced_to_its_text(self):
        html = b'<html><nav>Menu</nav><h1>Backend Engineer</h1><p>Python, Django</p></html>'
        with _jobs_host_is_public(), _serve(
            lambda req: httpx.Response(200, headers={'content-type': 'text/html'}, content=html)
        ):
            resp = self.scrape()

        self.assertEqual(resp.status_code, 200)
        text = resp.json()['text']
        self.assertIn('Backend Engineer', text)
        self.assertNotIn('Menu', text)

    def test_a_huge_page_is_not_read_past_the_cap(self):
        """The cap used to apply after resp.read() had already pulled everything in."""
        chunks_sent = []

        async def five_megabytes():
            for _ in range(80):
                chunks_sent.append(1)
                yield b'<p>' + b'x' * (64 * 1024) + b'</p>'

        with _jobs_host_is_public(), _serve(
            lambda req: httpx.Response(200, headers={'content-type': 'text/html'},
                                       content=five_megabytes())
        ):
            resp = self.scrape()

        self.assertEqual(resp.status_code, 200)
        self.assertLess(len(chunks_sent), 20, 'kept reading long after the 512 KB cap')

    def test_a_page_that_never_finishes_hits_the_deadline(self):
        """A byte every so often defeated the per-read timeout indefinitely."""
        async def trickle():
            while True:
                yield b'<p>x</p>'
                await asyncio.sleep(0.05)

        with patch('generator.views._SCRAPE_DEADLINE', 0.3), _jobs_host_is_public(), _serve(
            lambda req: httpx.Response(200, headers={'content-type': 'text/html'}, content=trickle())
        ):
            resp = self.scrape()

        self.assertEqual(resp.status_code, 400)
        self.assertIn('too long', resp.json()['error'])

    def test_a_redirect_into_the_private_network_is_refused(self):
        def handler(request):
            return httpx.Response(302, headers={'location': 'http://127.0.0.1/admin'})

        with _jobs_host_is_public(), _serve(handler):
            resp = self.scrape()

        self.assertEqual(resp.status_code, 400)
        self.assertIn('private', resp.json()['error'])

    def test_non_standard_ports_are_refused_before_any_lookup(self):
        for url in ('http://example.com:8080/job', 'http://example.com:8000/job'):
            with self.subTest(url=url), patch('socket.getaddrinfo') as lookup:
                ok, reason = _url_points_to_public_host(url)
                self.assertFalse(ok)
                self.assertIn('port', reason)
                lookup.assert_not_called()


# ---------------------------------------------------------------------------
# Anthropic client
# ---------------------------------------------------------------------------

def _fake_anthropic(behaviour):
    """
    A stand-in `anthropic` module. `behaviour(call_number)` returns a message
    or raises one of the module's exception classes.
    """
    mod = types.ModuleType('anthropic')
    state = {'calls': 0, 'clients': []}

    class _Err(Exception):
        def __init__(self, msg='', status_code=None):
            super().__init__(msg)
            self.status_code = status_code

    for name in ('RateLimitError', 'APIConnectionError', 'APIStatusError', 'BadRequestError'):
        setattr(mod, name, type(name, (_Err,), {}))

    async def create(**kw):
        state['calls'] += 1
        return behaviour(mod, state['calls'])

    def make_client(**opts):
        state['clients'].append(opts)
        return types.SimpleNamespace(messages=types.SimpleNamespace(create=create))

    mod.AsyncAnthropic = make_client
    return mod, state


def _message(text='ok', input_tokens=100, output_tokens=50):
    return types.SimpleNamespace(
        stop_reason='end_turn', stop_details=None,
        usage=types.SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
        content=[types.SimpleNamespace(type='text', text=text)],
    )


async def _call():
    return await ai_client.call_anthropic('sys', 'usr', api_key='sk-test', model='m')


class AnthropicResilienceTest(_CleanCache):

    async def test_one_client_per_loop_with_a_bounded_timeout(self):
        mod, state = _fake_anthropic(lambda m, n: _message())
        with patch.dict(sys.modules, {'anthropic': mod}):
            await _call()
            await _call()

        self.assertEqual(len(state['clients']), 1, 'a new client (and TLS handshake) per call')
        opts = state['clients'][0]
        self.assertEqual(opts['max_retries'], 1)
        self.assertLessEqual(opts['timeout'].read, 300, 'the SDK default is 600s')

    async def test_repeated_provider_failures_open_the_breaker(self):
        def down(mod, n):
            raise mod.APIConnectionError('connection refused')

        mod, state = _fake_anthropic(down)
        with patch.dict(sys.modules, {'anthropic': mod}):
            for _ in range(ai_client.BREAKER_THRESHOLD):
                with self.assertRaises(ai_client.AIClientError):
                    await _call()
            calls_before = state['calls']

            with self.assertRaises(ai_client.AIClientError) as ctx:
                await _call()

        self.assertEqual(state['calls'], calls_before, 'an open breaker must not call out')
        self.assertIn('try again in a minute', str(ctx.exception))

    async def test_bad_requests_do_not_trip_the_breaker(self):
        def rejected(mod, n):
            raise mod.BadRequestError('invalid prompt', status_code=400)

        mod, state = _fake_anthropic(rejected)
        with patch.dict(sys.modules, {'anthropic': mod}):
            for _ in range(ai_client.BREAKER_THRESHOLD + 2):
                with self.assertRaises(ai_client.AIClientError):
                    await _call()

        self.assertEqual(state['calls'], ai_client.BREAKER_THRESHOLD + 2)

    async def test_overloaded_gets_its_own_message(self):
        def overloaded(mod, n):
            raise mod.APIStatusError('overloaded', status_code=529)

        mod, _ = _fake_anthropic(overloaded)
        with patch.dict(sys.modules, {'anthropic': mod}):
            with self.assertRaises(ai_client.AIClientError) as ctx:
                await _call()
        self.assertIn('overloaded', str(ctx.exception))

    async def test_usage_is_counted_per_day(self):
        mod, _ = _fake_anthropic(lambda m, n: _message(input_tokens=120, output_tokens=30))
        with patch.dict(sys.modules, {'anthropic': mod}):
            await _call()
            await _call()
        self.assertEqual(await cache.aget(ai_client._tokens_key()), 300)

    @override_settings(AI_DAILY_TOKEN_BUDGET=1000)
    async def test_a_spent_daily_budget_stops_calls(self):
        await cache.aset(ai_client._tokens_key(), 1000)
        mod, state = _fake_anthropic(lambda m, n: _message())
        with patch.dict(sys.modules, {'anthropic': mod}):
            with self.assertRaises(ai_client.AIClientError) as ctx:
                await _call()
        self.assertEqual(state['calls'], 0)
        self.assertIn('paused for today', str(ctx.exception))


# ---------------------------------------------------------------------------
# Sign-in and sign-up limits
# ---------------------------------------------------------------------------

class ClientIpTest(TestCase):

    def test_cloudflare_header_wins_over_a_spoofable_forwarded_for(self):
        request = RequestFactory().get(
            '/', HTTP_X_FORWARDED_FOR='1.2.3.4', HTTP_CF_CONNECTING_IP='203.0.113.9',
            REMOTE_ADDR='10.0.0.1',
        )
        self.assertEqual(client_ip(request), '203.0.113.9')

    def test_forwarded_for_alone_is_not_trusted(self):
        request = RequestFactory().get('/', HTTP_X_FORWARDED_FOR='1.2.3.4', REMOTE_ADDR='10.0.0.1')
        self.assertEqual(client_ip(request), '10.0.0.1')


class LoginThrottleTest(_CleanCache):

    def setUp(self):
        super().setUp()
        User.objects.create_user('victim', password='correct-horse-battery')

    def attempt(self, password, ip='198.51.100.7', username='victim'):
        return Client().post(reverse('login'), {'username': username, 'password': password},
                             HTTP_CF_CONNECTING_IP=ip)

    def test_a_correct_password_still_works_under_the_limit(self):
        for _ in range(3):
            self.attempt('wrong')
        resp = self.attempt('correct-horse-battery')
        self.assertEqual(resp.status_code, 302)

    def test_repeated_failures_lock_the_account_even_for_the_right_password(self):
        for _ in range(user_views.LOGIN_FAILURES_PER_USERNAME):
            self.attempt('wrong', ip=f'198.51.100.{_}')   # spread across addresses

        resp = self.attempt('correct-horse-battery', ip='192.0.2.1')

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Too many failed sign-in attempts')
        self.assertNotIn('sessionid', resp.cookies)

    def test_one_address_spraying_many_usernames_is_locked_out(self):
        for i in range(user_views.LOGIN_FAILURES_PER_IP):
            self.attempt('wrong', username=f'user{i}')

        resp = self.attempt('correct-horse-battery')

        self.assertContains(resp, 'Too many failed sign-in attempts')


class RegistrationLimitTest(_CleanCache):

    def register(self, n, ip):
        return Client().post(reverse('register'), {
            'username': f'new{n}', 'email': f'new{n}@example.com',
            'password1': 'Str0ng-pass-4839', 'password2': 'Str0ng-pass-4839',
        }, HTTP_CF_CONNECTING_IP=ip, HTTP_X_FORWARDED_FOR=f'10.9.{n}.1')

    def test_the_limit_holds_against_a_rotating_forwarded_for(self):
        for n in range(user_views.REGISTRATIONS_PER_HOUR):
            self.assertEqual(self.register(n, '203.0.113.5').status_code, 302)

        resp = self.register(99, '203.0.113.5')

        self.assertContains(resp, 'Too many accounts')
        self.assertFalse(User.objects.filter(username='new99').exists())

    def test_another_address_is_unaffected(self):
        for n in range(user_views.REGISTRATIONS_PER_HOUR):
            self.register(n, '203.0.113.5')
        self.assertEqual(self.register(50, '203.0.113.6').status_code, 302)


# ---------------------------------------------------------------------------
# Account deletion
# ---------------------------------------------------------------------------

class DeleteAccountTest(TestCase):

    def setUp(self):
        self.user = User.objects.create_user('leaver', password='pw-12345')
        Profile.objects.filter(user=self.user).update(plan='pro', stripe_customer_id='cus_1')
        self.client = Client()
        self.client.force_login(self.user)

    def delete(self):
        return self.client.post(reverse('delete_account'))

    def test_the_account_is_kept_if_billing_cannot_be_stopped(self):
        with patch('users.views.stripe.Customer.delete',
                   side_effect=stripe.error.APIConnectionError('Stripe is down')):
            resp = self.delete()

        self.assertEqual(resp.status_code, 503)
        self.assertIn('not been deleted', resp.json()['error'])
        self.assertTrue(User.objects.filter(pk=self.user.pk).exists())

    def test_a_customer_already_deleted_on_stripe_does_not_block_deletion(self):
        missing = stripe.error.InvalidRequestError('No such customer', 'id', code='resource_missing')
        with patch('users.views.stripe.Customer.delete', side_effect=missing):
            resp = self.delete()

        self.assertEqual(resp.status_code, 200)
        self.assertFalse(User.objects.filter(pk=self.user.pk).exists())

    def test_deletion_cancels_billing_then_removes_the_user(self):
        with patch('users.views.stripe.Customer.delete') as delete_customer:
            resp = self.delete()

        delete_customer.assert_called_once_with('cus_1')
        self.assertEqual(resp.json(), {'status': 'success'})
        self.assertFalse(User.objects.filter(pk=self.user.pk).exists())

    def test_a_user_without_billing_is_deleted_without_calling_stripe(self):
        Profile.objects.filter(user=self.user).update(stripe_customer_id=None)
        with patch('users.views.stripe.Customer.delete') as delete_customer:
            self.delete()

        delete_customer.assert_not_called()
        self.assertFalse(User.objects.filter(pk=self.user.pk).exists())
