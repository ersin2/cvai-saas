"""
Input validation on the tracker and profile forms, the billing pages' error
handling, and the pages that report a user's remaining quota.
"""

from unittest.mock import patch

import stripe
from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from generator.guards import RATE_LIMITS, _check_rate_limit
from generator.models import JobApplication
from users.models import Profile, avatar_upload_to


class _LoggedIn(TestCase):
    plan = 'free'

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user('formuser', password='pw-12345')
        Profile.objects.filter(user=self.user).update(plan=self.plan)
        self.client = Client()
        self.client.force_login(self.user)

    def tearDown(self):
        cache.clear()

    def messages_of(self, resp):
        return [str(m) for m in get_messages(resp.wsgi_request)]


class TrackerFormTest(_LoggedIn):

    def add(self, **fields):
        data = {'company_name': 'Northwind', 'job_title': 'Engineer', 'status': 'saved', **fields}
        return self.client.post(reverse('tracker'), data)

    def test_a_valid_application_is_saved(self):
        self.add(job_url='https://jobs.example/1')
        self.assertTrue(JobApplication.objects.filter(user=self.user, company_name='Northwind').exists())

    def test_a_javascript_url_is_rejected(self):
        """Rendered as the card's href, this ran script on click."""
        resp = self.add(job_url='javascript:alert(document.cookie)')
        self.assertFalse(JobApplication.objects.exists())
        self.assertTrue(self.messages_of(resp))

    def test_an_unknown_status_is_rejected(self):
        self.add(status='hired-by-magic')
        self.assertFalse(JobApplication.objects.exists())

    def test_a_value_longer_than_its_column_is_rejected_not_a_500(self):
        resp = self.add(company_name='x' * 300)
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(JobApplication.objects.exists())

    def test_an_old_javascript_url_is_not_rendered_as_a_link(self):
        JobApplication.objects.create(user=self.user, company_name='Old', job_title='Row',
                                      job_url='javascript:alert(1)')
        JobApplication.objects.create(user=self.user, company_name='Good', job_title='Row',
                                      job_url='https://jobs.example/1')
        resp = self.client.get(reverse('tracker'))
        self.assertNotContains(resp, 'href="javascript:')
        self.assertContains(resp, 'href="https://jobs.example/1"')


class ProfileFormTest(_LoggedIn):

    def test_an_unknown_font_is_rejected(self):
        resp = self.client.post(reverse('profile'), {
            'base_resume': 'CV', 'default_font': 'x' * 80, 'default_language': 'English',
        })
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(Profile.objects.get(user=self.user).default_font, 'Inter')
        self.assertTrue(any('Could not save' in m for m in self.messages_of(resp)))

    def test_avatar_names_are_random(self):
        name = avatar_upload_to(None, 'Ivan_Petrov_passport.JPG')
        self.assertTrue(name.startswith('avatars/'))
        self.assertTrue(name.endswith('.jpg'))
        self.assertNotIn('Petrov', name)


@override_settings(STRIPE_SECRET_KEY='sk_test')
class BillingPagesTest(_LoggedIn):

    def test_payment_success_without_a_session_does_not_congratulate(self):
        resp = self.client.get(reverse('payment_success'))
        self.assertRedirects(resp, reverse('pricing'), fetch_redirect_response=False)

    def test_payment_success_for_someone_elses_session_does_not_congratulate(self):
        session = {'client_reference_id': '999999', 'status': 'complete'}
        with patch('users.views.stripe.checkout.Session.retrieve', return_value=session):
            resp = self.client.get(reverse('payment_success') + '?session_id=cs_x')
        self.assertRedirects(resp, reverse('pricing'), fetch_redirect_response=False)

    def test_payment_success_for_a_completed_session(self):
        session = {'client_reference_id': str(self.user.id), 'status': 'complete'}
        with patch('users.views.stripe.checkout.Session.retrieve', return_value=session):
            resp = self.client.get(reverse('payment_success') + '?session_id=cs_x')
        self.assertRedirects(resp, reverse('profile'), fetch_redirect_response=False)
        self.assertTrue(any('Payment received' in m for m in self.messages_of(resp)))

    def test_a_portal_failure_is_not_shown_as_raw_exception_text(self):
        Profile.objects.filter(user=self.user).update(stripe_customer_id='cus_1')
        with patch('users.views.stripe.billing_portal.Session.create',
                   side_effect=stripe.error.APIConnectionError('internal detail sk_live_xyz')):
            resp = self.client.get(reverse('stripe_customer_portal'))

        self.assertRedirects(resp, reverse('profile'), fetch_redirect_response=False)
        shown = ' '.join(self.messages_of(resp))
        self.assertNotIn('internal detail', shown)
        self.assertIn('billing portal', shown)


class ProQuotaIsShownTest(_LoggedIn):
    plan = 'pro'

    def test_the_dashboard_shows_a_number_not_infinity(self):
        resp = self.client.get(reverse('dashboard'))
        self.assertEqual(resp.context['stats']['generations_left'], Profile.PLAN_LIMITS['pro'])
        self.assertContains(resp, f"{Profile.PLAN_LIMITS['pro']} / {Profile.PLAN_LIMITS['pro']} left")


class UtilityBucketTest(_LoggedIn):

    def test_a_spent_ai_bucket_does_not_block_reading_a_pdf(self):
        for _ in range(RATE_LIMITS['free'] + 1):
            _check_rate_limit(self.user, 'free')

        resp = self.client.post(reverse('parse_resume_pdf'), {})

        self.assertNotEqual(resp.status_code, 429)
        self.assertEqual(resp.json()['error'], 'No file uploaded.')
