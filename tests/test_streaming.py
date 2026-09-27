"""
Streamed generation: the cover letter and the three Tools send their text as
it is written (text/event-stream), and the reservation moves into the stream.

The provider is replaced by a fake SDK module, so these run the real
stream_anthropic(), _event_stream() and quota code end to end.
"""

import asyncio
import json
import sys
import types
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import AsyncClient, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from generator.models import AIResult, AIUsageDay, Generation
from users.models import Profile

STREAM = {'headers': {'Accept': 'text/event-stream'}}


def _fake_anthropic(fragments, *, stop_reason='end_turn', fail_after=None, hang_after=None):
    """
    An `anthropic` module whose messages.stream() emits a thinking event, then
    `fragments` as text events. fail_after=N raises a 529 after N fragments;
    hang_after=N waits forever after N fragments (a slow model).
    """
    mod = types.ModuleType('anthropic')

    class _Err(Exception):
        pass

    class APIStatusError(_Err):
        def __init__(self, status_code):
            super().__init__(f'status {status_code}')
            self.status_code = status_code

    mod.APIStatusError = APIStatusError
    for name in ('RateLimitError', 'APIConnectionError', 'BadRequestError'):
        setattr(mod, name, type(name, (_Err,), {}))

    class _Stream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def __aiter__(self):
            yield types.SimpleNamespace(type='thinking')
            for i, text in enumerate(fragments):
                if fail_after is not None and i == fail_after:
                    raise APIStatusError(529)
                if hang_after is not None and i == hang_after:
                    await asyncio.Event().wait()
                yield types.SimpleNamespace(type='content_block_delta')
                yield types.SimpleNamespace(type='text', text=text)

        async def get_final_message(self):
            return types.SimpleNamespace(
                stop_reason=stop_reason, stop_details=None,
                usage=types.SimpleNamespace(input_tokens=100, output_tokens=len(fragments)))

    mod.AsyncAnthropic = lambda **kw: types.SimpleNamespace(
        messages=types.SimpleNamespace(stream=lambda **kw: _Stream()))
    return mod


def _events(raw):
    """Parse an event-stream body into [(event, payload)]; comments are dropped."""
    out = []
    for block in raw.split('\n\n'):
        event, data = None, ''
        for line in block.split('\n'):
            if line.startswith('event:'):
                event = line[6:].strip()
            elif line.startswith('data:'):
                data += line[5:].strip()
        if event:
            out.append((event, json.loads(data)))
    return out


async def _read(response):
    return b''.join([chunk async for chunk in response.streaming_content]).decode()


@override_settings(ANTHROPIC_API_KEY='test-key')
class StreamedToolsTest(TestCase):

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user('streamer', password='pw-12345')
        self.profile, _ = Profile.objects.get_or_create(user=self.user)
        self.profile.generations_count = 3
        self.profile.save()
        self.client = AsyncClient()

    def tearDown(self):
        cache.clear()

    async def remaining(self):
        await self.profile.arefresh_from_db()
        return self.profile.generations_remaining()

    async def _post(self, name, fake, data=None, **extra):
        await self.client.aforce_login(self.user)
        with patch.dict(sys.modules, {'anthropic': fake}):
            resp = await self.client.post(reverse(name), data or {'resume': 'r', 'job_description': 'jd'},
                                          **{**STREAM, **extra})
            body = await _read(resp) if resp.streaming else None
        return resp, body

    async def test_text_arrives_in_order_then_done_with_the_saved_result(self):
        fragments = ['ATS COMPATIBILITY SCORE: ', '81', '/100\n\n', '**Matched:** Python']
        resp, body = await self._post('ats_score', _fake_anthropic(fragments))

        self.assertEqual(resp['Content-Type'], 'text/event-stream')
        self.assertEqual(resp['Cache-Control'], 'no-cache')
        self.assertEqual(resp['X-Accel-Buffering'], 'no')
        events = _events(body)
        deltas = [p['text'] for e, p in events if e == 'delta']
        self.assertEqual(deltas, fragments, 'each fragment is sent as it arrives, in order')
        self.assertEqual(events[-1][0], 'done')
        done = events[-1][1]
        self.assertEqual(done['result'], ''.join(fragments))
        self.assertEqual((done['kind'], done['ats_score']), ('ats', 81))
        self.assertIn('tool-result-card', done['recent_html'])

        row = await AIResult.objects.aget(user=self.user)
        self.assertEqual((row.result_type, row.score), ('ats', 81))
        self.assertEqual(await self.remaining(), 2, 'a delivered answer costs one generation')
        usage = await AIUsageDay.objects.aget(date=timezone.now().date())
        self.assertEqual((usage.calls, usage.input_tokens), (1, 100))

    async def test_a_provider_failure_mid_stream_is_an_error_event_and_refunded(self):
        resp, body = await self._post('interview_prep', _fake_anthropic(['### Q1', ' more', ' text'], fail_after=2))

        events = _events(body)
        self.assertEqual([e for e, _ in events], ['delta', 'delta', 'error'])
        self.assertIn('overloaded', events[-1][1]['error'])
        self.assertFalse(await AIResult.objects.aexists())
        self.assertEqual(await self.remaining(), 3)

    async def test_a_refusal_after_partial_text_is_not_saved(self):
        resp, body = await self._post('followup_email', _fake_anthropic(['Subject: '], stop_reason='refusal'),
                                      data={'company_name': 'Acme', 'job_title': 'Eng'})

        self.assertEqual(_events(body)[-1], ('error', {'error': 'The AI declined this request. Please rephrase and try again.'}))
        self.assertFalse(await AIResult.objects.aexists())
        self.assertEqual(await self.remaining(), 3)

    async def test_a_browser_that_leaves_mid_answer_is_refunded(self):
        """Django cancels the response task when the client disconnects."""
        await self.client.aforce_login(self.user)
        with patch.dict(sys.modules, {'anthropic': _fake_anthropic(['one ', 'two'], hang_after=1)}):
            resp = await self.client.post(reverse('interview_prep'), {'resume': 'r', 'job_description': 'j'}, **STREAM)
            got_text = asyncio.Event()

            async def consume():
                async for chunk in resp.streaming_content:
                    if b'event: delta' in chunk:
                        got_text.set()

            task = asyncio.create_task(consume())
            await asyncio.wait_for(got_text.wait(), 5)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertFalse(await AIResult.objects.aexists())
        self.assertEqual(await self.remaining(), 3)

    async def test_quota_and_throttle_answer_before_any_stream(self):
        self.profile.generations_count = 0
        await self.profile.asave()
        resp, body = await self._post('ats_score', _fake_anthropic(['x']))
        self.assertEqual(resp.status_code, 402)
        self.assertFalse(resp.streaming)
        self.assertIn('error', json.loads(resp.content))

    @override_settings(ANTHROPIC_API_KEY='')
    async def test_missing_key_is_an_error_event(self):
        resp, body = await self._post('ats_score', _fake_anthropic(['x']))
        self.assertIn('not configured', _events(body)[-1][1]['error'])
        self.assertEqual(await self.remaining(), 3)


@override_settings(ANTHROPIC_API_KEY='test-key')
class StreamedCoverLetterTest(TestCase):

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user('writer', password='pw-12345', first_name='Ada')
        profile, _ = Profile.objects.get_or_create(user=self.user)
        profile.generations_count = 3
        profile.save()

    async def test_streams_and_saves_to_history(self):
        client = AsyncClient()
        await client.aforce_login(self.user)
        letter = ['[SECTION: MAIN_LETTER]', 'Dear Acme,', ' hello.', '[END_SECTION]']
        with patch.dict(sys.modules, {'anthropic': _fake_anthropic(letter)}):
            resp = await client.post(reverse('generate_letter'),
                                     {'resume': 'cv', 'job_description': 'jd', 'company_name': 'Acme'}, **STREAM)
            events = _events(await _read(resp))

        self.assertEqual([p['text'] for e, p in events if e == 'delta'], letter)
        self.assertEqual(events[-1], ('done', {'result': ''.join(letter), 'error': None}))
        gen = await Generation.objects.aget(user=self.user)
        self.assertEqual(gen.company_name, 'Acme')

    async def test_without_the_stream_header_the_answer_is_json_as_before(self):
        client = AsyncClient()
        await client.aforce_login(self.user)
        with patch.dict(sys.modules, {'anthropic': types.ModuleType('anthropic')}), \
                patch('generator.views._call_ai_service', return_value=('[SECTION: MAIN_LETTER]Hi[END_SECTION]', None)):
            resp = await client.post(reverse('generate_letter'), {'resume': 'cv', 'job_description': 'jd'})
        self.assertFalse(resp.streaming)
        self.assertEqual(resp.json()['result'], '[SECTION: MAIN_LETTER]Hi[END_SECTION]')
