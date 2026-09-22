"""
Stripe billing: the webhook, checkout creation, and the profile save that
used to be able to undo them.

No test talks to Stripe. The webhook tests replace signature verification
with a fixed event, which is the only Stripe call on that path.
"""

from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import DatabaseError
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from users import views as user_views
from users.models import Profile, StripeEvent


PRICES = {'pro': 'price_pro_test', 'elite': 'price_elite_test'}


def _event(event_id, type_, obj):
    return {'id': event_id, 'type': type_, 'data': {'object': obj}}


def _checkout(user, plan='pro', sub='sub_1', customer='cus_1'):
    return {
        'client_reference_id': str(user.id), 'metadata': {'plan': plan},
        'customer': customer, 'subscription': sub,
    }


def _subscription(sub='sub_1', customer='cus_1', status='active', price='price_pro_test'):
    return {
        'id': sub, 'customer': customer, 'status': status,
        'items': {'data': [{'price': {'id': price}}]},
    }


@override_settings(STRIPE_WEBHOOK_SECRET='whsec_test')
@patch.dict(user_views.PLAN_PRICE_IDS, PRICES)
class StripeWebhookTest(TestCase):

    def setUp(self):
        self.user = User.objects.create_user('payer', password='pw-12345')
        self.profile, _ = Profile.objects.get_or_create(user=self.user)
        self.client = Client(raise_request_exception=False)

    def deliver(self, event):
        with patch('users.views.stripe.Webhook.construct_event', return_value=event):
            return self.client.post(reverse('stripe_webhook'), data=b'{}',
                                    content_type='application/json',
                                    HTTP_STRIPE_SIGNATURE='t=1,v1=x')

    def plan(self):
        self.profile.refresh_from_db()
        return self.profile.plan

    def test_checkout_upgrades_and_records_the_subscription(self):
        resp = self.deliver(_event('evt_1', 'checkout.session.completed', _checkout(self.user)))

        self.assertEqual(resp.status_code, 200)
        self.profile.refresh_from_db()
        self.assertEqual(
            (self.profile.plan, self.profile.stripe_customer_id, self.profile.stripe_subscription_id),
            ('pro', 'cus_1', 'sub_1'),
        )

    def test_a_duplicate_delivery_is_not_applied_twice(self):
        event = _event('evt_dup', 'checkout.session.completed', _checkout(self.user))
        self.deliver(event)
        Profile.objects.filter(pk=self.profile.pk).update(plan='free')  # e.g. refunded since

        resp = self.deliver(event)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.plan(), 'free')

    def test_a_delivery_that_fails_is_applied_by_the_retry(self):
        """
        The cache marker used to be set before the change: a failure left the
        event marked as seen, and Stripe's retry was answered 200 and dropped.
        """
        event = _event('evt_retry', 'checkout.session.completed', _checkout(self.user))
        with patch.dict(user_views._EVENT_HANDLERS,
                        {'checkout.session.completed': _raise_db_error}):
            failed = self.deliver(event)

        self.assertEqual(failed.status_code, 500, 'a 500 is what makes Stripe retry')
        self.assertFalse(StripeEvent.objects.filter(event_id='evt_retry').exists())
        self.assertEqual(self.plan(), 'free')

        retried = self.deliver(event)

        self.assertEqual(retried.status_code, 200)
        self.assertEqual(self.plan(), 'pro')

    def test_deleting_a_stale_subscription_keeps_the_current_plan(self):
        """A user who switched subscriptions and cancelled the old one is still paying."""
        self.deliver(_event('evt_a', 'checkout.session.completed',
                            _checkout(self.user, plan='elite', sub='sub_new')))

        self.deliver(_event('evt_b', 'customer.subscription.deleted',
                            _subscription(sub='sub_old')))

        self.assertEqual(self.plan(), 'elite')

    def test_deleting_the_current_subscription_downgrades(self):
        self.deliver(_event('evt_a', 'checkout.session.completed', _checkout(self.user)))
        self.deliver(_event('evt_b', 'customer.subscription.deleted', _subscription()))

        self.profile.refresh_from_db()
        self.assertEqual((self.profile.plan, self.profile.stripe_subscription_id), ('free', ''))

    def test_a_legacy_profile_without_a_subscription_id_matches_by_customer(self):
        Profile.objects.filter(pk=self.profile.pk).update(plan='pro', stripe_customer_id='cus_1')
        self.deliver(_event('evt_b', 'customer.subscription.deleted', _subscription()))
        self.assertEqual(self.plan(), 'free')

    def test_a_portal_plan_switch_changes_the_plan(self):
        self.deliver(_event('evt_a', 'checkout.session.completed', _checkout(self.user)))
        self.deliver(_event('evt_b', 'customer.subscription.updated',
                            _subscription(price='price_elite_test')))
        self.assertEqual(self.plan(), 'elite')

    def test_an_unpaid_subscription_loses_the_plan_but_past_due_keeps_it(self):
        self.deliver(_event('evt_a', 'checkout.session.completed', _checkout(self.user)))

        self.deliver(_event('evt_b', 'customer.subscription.updated',
                            _subscription(status='past_due')))
        self.assertEqual(self.plan(), 'pro', "Stripe is still retrying the card")

        self.deliver(_event('evt_c', 'customer.subscription.updated',
                            _subscription(status='unpaid')))
        self.assertEqual(self.plan(), 'free')

    def test_an_upgrade_does_not_rewind_the_quota_counter(self):
        Profile.objects.filter(pk=self.profile.pk).update(generations_count=1)
        self.deliver(_event('evt_a', 'checkout.session.completed', _checkout(self.user)))
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.generations_count, 1)


def _raise_db_error(_obj):
    raise DatabaseError('connection dropped')


@override_settings(STRIPE_SECRET_KEY='sk_test')
@patch.dict(user_views.PLAN_PRICE_IDS, PRICES)
class BuyPremiumTest(TestCase):

    def setUp(self):
        self.user = User.objects.create_user('buyer', email='b@example.com', password='pw-12345')
        self.profile, _ = Profile.objects.get_or_create(user=self.user)
        self.client = Client()
        self.client.force_login(self.user)

    def test_a_subscriber_changing_plan_goes_to_the_portal_not_a_new_checkout(self):
        Profile.objects.filter(pk=self.profile.pk).update(plan='pro', stripe_customer_id='cus_1')
        with patch('users.views.stripe.checkout.Session.create') as create:
            resp = self.client.get(reverse('buy_premium', args=['elite']))

        create.assert_not_called()
        self.assertRedirects(resp, reverse('stripe_customer_portal'),
                             fetch_redirect_response=False)

    def test_a_returning_customer_is_reused(self):
        Profile.objects.filter(pk=self.profile.pk).update(stripe_customer_id='cus_old')
        with patch('users.views.stripe.checkout.Session.create') as create:
            create.return_value.url = 'https://checkout.stripe.test/s'
            self.client.get(reverse('buy_premium', args=['pro']))

        kwargs = create.call_args.kwargs
        self.assertEqual(kwargs['customer'], 'cus_old')
        self.assertNotIn('customer_email', kwargs)

    def test_a_first_time_buyer_is_identified_by_email(self):
        with patch('users.views.stripe.checkout.Session.create') as create:
            create.return_value.url = 'https://checkout.stripe.test/s'
            self.client.get(reverse('buy_premium', args=['pro']))

        kwargs = create.call_args.kwargs
        self.assertEqual(kwargs['customer_email'], 'b@example.com')
        self.assertNotIn('customer', kwargs)


class ProfileSaveTest(TestCase):

    def test_saving_preferences_does_not_overwrite_the_plan(self):
        """
        The view read the profile at the start of the request and save()d every
        column at the end, so a plan upgrade landing in between was undone.
        """
        user = User.objects.create_user('prefs', password='pw-12345')
        profile, _ = Profile.objects.get_or_create(user=user)
        stale = Profile.objects.get(pk=profile.pk)
        Profile.objects.filter(pk=profile.pk).update(plan='elite', is_premium=True)

        client = Client()
        client.force_login(user)
        with patch.object(Profile.objects, 'get_or_create', return_value=(stale, False)):
            client.post(reverse('profile'), {
                'base_resume': 'My resume', 'default_font': 'Inter', 'default_language': 'English',
            })

        profile.refresh_from_db()
        self.assertEqual(profile.plan, 'elite')
        self.assertEqual(profile.base_resume, 'My resume')
