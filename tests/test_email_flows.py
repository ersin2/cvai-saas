"""
Password reset and email confirmation, end to end through the outbox.

Django's test runner swaps in the in-memory mail backend, so every link a
test follows is the one a user would have received.
"""

import re
from unittest.mock import AsyncMock, patch

from django.contrib.auth.models import User
from django.core import mail
from django.core.cache import cache
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from users.models import Profile


def _link(message):
    return re.search(r'https?://\S+', message.body).group(0)


class _Clean(TestCase):
    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()


@override_settings(EMAIL_CONFIGURED=True)
class PasswordResetTest(_Clean):

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user('forgetful', email='forgetful@example.com',
                                             password='old-pass-9281')

    def test_the_emailed_link_sets_a_new_password(self):
        resp = Client().post(reverse('password_reset'), {'email': 'Forgetful@Example.com'})
        self.assertRedirects(resp, reverse('password_reset_done'))
        self.assertEqual(len(mail.outbox), 1)

        c = Client()
        form_page = c.get(_link(mail.outbox[0]), follow=True)
        self.assertContains(form_page, 'Choose a new password')
        done = c.post(form_page.redirect_chain[-1][0], {
            'new_password1': 'N3w-pass-5710', 'new_password2': 'N3w-pass-5710',
        })
        self.assertRedirects(done, reverse('password_reset_complete'))
        self.assertTrue(Client().login(username='forgetful', password='N3w-pass-5710'))

    def test_an_unknown_address_gets_the_same_answer_and_no_email(self):
        resp = Client().post(reverse('password_reset'), {'email': 'nobody@example.com'})
        self.assertRedirects(resp, reverse('password_reset_done'))
        self.assertEqual(len(mail.outbox), 0)

    def test_requests_are_throttled_per_address(self):
        c = Client(HTTP_CF_CONNECTING_IP='203.0.113.40')
        for _ in range(5):
            c.post(reverse('password_reset'), {'email': 'forgetful@example.com'})
        resp = c.post(reverse('password_reset'), {'email': 'forgetful@example.com'})

        self.assertContains(resp, 'Too many reset requests')
        self.assertEqual(len(mail.outbox), 5)

    def test_the_login_page_links_to_reset(self):
        self.assertContains(Client().get(reverse('login')), reverse('password_reset'))


@override_settings(EMAIL_CONFIGURED=False)
class PasswordResetWithoutEmailTest(_Clean):

    def test_the_page_says_reset_is_unavailable_and_sends_nothing(self):
        User.objects.create_user('x', email='x@example.com', password='pw-12345-abc')
        resp = Client().post(reverse('password_reset'), {'email': 'x@example.com'})
        self.assertContains(resp, "isn't available yet")
        self.assertEqual(len(mail.outbox), 0)


@override_settings(REQUIRE_EMAIL_VERIFICATION=True)
class EmailConfirmationTest(_Clean):

    def register(self):
        c = Client(HTTP_CF_CONNECTING_IP='203.0.113.50')
        c.post(reverse('register'), {
            'username': 'newbie', 'email': 'newbie@example.com',
            'password1': 'Str0ng-pass-4839', 'password2': 'Str0ng-pass-4839',
        })
        return c

    def rewrite(self, c):
        with patch('generator.views._call_ai_service', new_callable=AsyncMock,
                   return_value=('Sharper summary.', None)) as ai:
            resp = c.post(reverse('rewrite_section'),
                          {'text': 'Did backend work.', 'section_type': 'summary'})
        return resp, ai

    def test_sign_up_sends_a_link_and_generation_waits_for_it(self):
        c = self.register()
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('newbie@example.com', mail.outbox[0].to)

        resp, ai = self.rewrite(c)
        self.assertEqual(resp.status_code, 402)
        self.assertIn('Confirm your email', resp.json()['error'])
        ai.assert_not_awaited()
        self.assertEqual(Profile.objects.get(user__username='newbie').generations_count, 3,
                         'blocked before a generation was spent')

        confirmed = Client().get(_link(mail.outbox[0]))   # e.g. opened on a phone
        self.assertContains(confirmed, 'Email confirmed')

        resp, ai = self.rewrite(c)
        self.assertEqual(resp.status_code, 200)
        ai.assert_awaited()

    def test_the_banner_offers_a_resend(self):
        c = self.register()
        page = c.get(reverse('dashboard'))
        self.assertContains(page, 'Confirm your email')

        c.post(reverse('resend_verification'))
        self.assertEqual(len(mail.outbox), 2)

    def test_resends_are_limited(self):
        c = self.register()
        for _ in range(5):
            c.post(reverse('resend_verification'))
        self.assertEqual(len(mail.outbox), 1 + 3)

    def test_a_link_for_an_old_address_does_not_confirm_the_new_one(self):
        self.register()
        User.objects.filter(username='newbie').update(email='changed@example.com')

        resp = Client().get(_link(mail.outbox[0]))

        self.assertContains(resp, 'expired')
        self.assertFalse(Profile.objects.get(user__username='newbie').email_verified)

    def test_a_tampered_link_is_rejected(self):
        self.register()
        resp = Client().get(reverse('verify_email', args=['not-a-real-token']))
        self.assertContains(resp, 'expired')

    def test_paying_subscribers_are_not_blocked(self):
        c = self.register()
        Profile.objects.filter(user__username='newbie').update(plan='pro')
        resp, ai = self.rewrite(c)
        self.assertEqual(resp.status_code, 200)


class ConfirmationOffByDefaultTest(_Clean):
    """Without email configured the requirement is off, so nobody is locked out."""

    def test_new_accounts_can_generate_straight_away(self):
        c = Client(HTTP_CF_CONNECTING_IP='203.0.113.60')
        c.post(reverse('register'), {
            'username': 'plain', 'email': 'plain@example.com',
            'password1': 'Str0ng-pass-4839', 'password2': 'Str0ng-pass-4839',
        })
        self.assertEqual(len(mail.outbox), 0)
        with patch('generator.views._call_ai_service', new_callable=AsyncMock,
                   return_value=('Sharper summary.', None)):
            resp = c.post(reverse('rewrite_section'),
                          {'text': 'Did backend work.', 'section_type': 'summary'})
        self.assertEqual(resp.status_code, 200)


class GrandfatherMigrationTest(TransactionTestCase):
    """Accounts that existed before confirmation was introduced keep working."""

    def test_existing_profiles_are_marked_confirmed(self):
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor

        executor = MigrationExecutor(connection)
        executor.migrate([('users', '0008_avatar_random_filenames')])
        old_apps = executor.loader.project_state([('users', '0008_avatar_random_filenames')]).apps
        OldUser = old_apps.get_model('auth', 'User')
        OldProfile = old_apps.get_model('users', 'Profile')
        OldProfile.objects.create(user=OldUser.objects.create(username='veteran'))

        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())

        self.assertTrue(Profile.objects.get(user__username='veteran').email_verified)
