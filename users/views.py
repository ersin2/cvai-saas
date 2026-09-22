import hashlib
import logging
import stripe

from django.core import signing
from django.core.mail import send_mail
from django.shortcuts import render, redirect
from django.template.loader import render_to_string
from django.urls import reverse, reverse_lazy
from django.utils.http import url_has_allowed_host_and_scheme
from .forms import ProfilePreferencesForm, UserRegisterForm
from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import JsonResponse, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.conf import settings

from generator.guards import client_ip, hit_rate_limit, rate_limit_reached

logger = logging.getLogger(__name__)

stripe.api_key = settings.STRIPE_SECRET_KEY


# ──────────────────────────────────────────────────────────────────────────────
# Plan → Stripe Price-ID  mapping
# Reads safely from settings; missing IDs raise a friendly error at checkout.
# ──────────────────────────────────────────────────────────────────────────────
PLAN_PRICE_IDS = {
    'pro':   getattr(settings, 'STRIPE_PRICE_ID_PRO',   ''),
    'elite': getattr(settings, 'STRIPE_PRICE_ID_ELITE', ''),
}

PLAN_DISPLAY = {
    'pro':   'Pro ($5/mo)',
    'elite': 'Elite ($10/mo)',
}


# ──────────────────────────────────────────────────────────────────────────────
# AUTH
# ──────────────────────────────────────────────────────────────────────────────

# Sign-ups per client address per hour. Each account comes with free
# generations, so unlimited sign-ups are unlimited paid model calls.
REGISTRATIONS_PER_HOUR = 10

# Failed sign-ins before a 15-minute lockout. Counted per username, which is
# what stops guessing one account's password, and per address, which stops
# spraying one password across many accounts. The address limit is looser
# because an office or a mobile carrier can put many people behind one IP.
LOGIN_FAILURES_PER_USERNAME = 10
LOGIN_FAILURES_PER_IP = 30
LOGIN_LOCKOUT_SECONDS = 15 * 60


def register(request):
    if request.method == 'POST':
        reg_key = f'rl_reg:{client_ip(request)}'
        if rate_limit_reached(reg_key, REGISTRATIONS_PER_HOUR):
            # Rendered on the page: register.html shows no flash messages, so
            # the messages.error() this used to send was never seen.
            return render(request, 'users/register.html', {
                'form': UserRegisterForm(),
                'throttle_error': 'Too many accounts have been created from this network. '
                                  'Please try again in an hour.',
            })

        form = UserRegisterForm(request.POST)
        if form.is_valid():
            user = form.save()
            hit_rate_limit(reg_key, REGISTRATIONS_PER_HOUR, 3600)
            login(request, user)
            if user.profile.needs_email_verification():
                _send_verification_email(request, user)
                messages.success(
                    request,
                    f'Welcome, {user.username}! We sent a confirmation link to '
                    f'{user.email} — open it to unlock your free generations.'
                )
            else:
                messages.success(
                    request,
                    f'Welcome, {user.username}! Your account is ready.'
                )
            # Straight to the Studio — the thing they signed up to use, and
            # where sign-in already lands. The dashboard is empty for a new
            # account.
            return redirect('home')
    else:
        form = UserRegisterForm()
    return render(request, 'users/register.html', {'form': form})


class ThrottledLoginView(auth_views.LoginView):
    """Django's LoginView, with a lockout after repeated failed sign-ins."""
    template_name = 'users/login.html'
    throttle_error = 'Too many failed sign-in attempts. Please wait 15 minutes and try again.'

    def _failure_keys(self):
        username = (self.request.POST.get('username') or '').strip().lower()
        # Hashed so the cache key never holds whatever was typed as a username.
        digest = hashlib.sha256(username.encode()).hexdigest()[:32]
        return (
            (f'rl_login_user:{digest}', LOGIN_FAILURES_PER_USERNAME),
            (f'rl_login_ip:{client_ip(self.request)}', LOGIN_FAILURES_PER_IP),
        )

    def post(self, request, *args, **kwargs):
        # Checked before authenticating, so a locked-out guess costs no
        # password hash and reveals nothing about the password.
        if any(rate_limit_reached(key, limit) for key, limit in self._failure_keys()):
            logger.warning('Login throttled for ip=%s', client_ip(request))
            return self.render_to_response(self.get_context_data(
                form=self.get_form_class()(request=request),
                throttle_error=self.throttle_error,
            ))
        return super().post(request, *args, **kwargs)

    def form_invalid(self, form):
        for key, limit in self._failure_keys():
            hit_rate_limit(key, limit, LOGIN_LOCKOUT_SECONDS)
        return super().form_invalid(form)


# ──────────────────────────────────────────────────────────────────────────────
# PASSWORD RESET
# ──────────────────────────────────────────────────────────────────────────────

# Reset emails per client address per hour. Each request sends a real email to
# whatever address is typed in, so unthrottled this is a free mail cannon.
PASSWORD_RESETS_PER_HOUR = 5


class ThrottledPasswordResetView(auth_views.PasswordResetView):
    template_name = 'users/password_reset_form.html'
    email_template_name = 'users/emails/password_reset_email.txt'
    subject_template_name = 'users/emails/password_reset_subject.txt'
    success_url = reverse_lazy('password_reset_done')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['email_configured'] = settings.EMAIL_CONFIGURED
        return context

    def post(self, request, *args, **kwargs):
        if not settings.EMAIL_CONFIGURED:
            # Nothing can be delivered; the page already says so.
            return self.get(request, *args, **kwargs)
        if hit_rate_limit(f'rl_pwreset:{client_ip(request)}', PASSWORD_RESETS_PER_HOUR, 3600):
            return self.render_to_response(self.get_context_data(
                throttle_error='Too many reset requests. Please try again in an hour.',
            ))
        return super().post(request, *args, **kwargs)


# ──────────────────────────────────────────────────────────────────────────────
# EMAIL CONFIRMATION
# ──────────────────────────────────────────────────────────────────────────────

_VERIFY_SALT = 'users.verify-email'
_VERIFY_MAX_AGE = 3 * 24 * 3600
VERIFICATION_EMAILS_PER_HOUR = 3


def _send_verification_email(request, user):
    """
    Email `user` a signed confirmation link. Returns True if it was handed to
    the mail backend.

    The token signs the address as well as the user, so a link sent before the
    address was changed cannot confirm the new one.
    """
    token = signing.dumps({'u': user.pk, 'e': user.email.lower()}, salt=_VERIFY_SALT)
    body = render_to_string('users/emails/verify_email.txt', {
        'user': user,
        'verify_url': request.build_absolute_uri(reverse('verify_email', args=[token])),
    })
    try:
        send_mail('Confirm your CVAI email', body, None, [user.email])
    except Exception:
        logger.exception('Could not send the confirmation email to user %s.', user.pk)
        return False
    return True


def verify_email(request, token):
    """The link in the confirmation email. Works signed out, from any browser."""
    from .models import Profile
    verified = False
    try:
        data = signing.loads(token, salt=_VERIFY_SALT, max_age=_VERIFY_MAX_AGE)
        verified = Profile.objects.filter(
            user_id=data['u'], user__email__iexact=data['e'],
        ).update(email_verified=True) == 1
    except (signing.BadSignature, KeyError, TypeError):
        pass
    return render(request, 'users/verify_email_result.html', {'verified': verified})


@login_required
@require_POST
def resend_verification(request):
    if not request.user.profile.needs_email_verification():
        messages.info(request, 'Your email is already confirmed.')
    elif hit_rate_limit(f'rl_verify:{request.user.pk}', VERIFICATION_EMAILS_PER_HOUR, 3600):
        messages.error(request, 'We have sent several links already. Please check your inbox, '
                                'including spam, or try again in an hour.')
    elif _send_verification_email(request, request.user):
        messages.success(request, f'We sent a new confirmation link to {request.user.email}.')
    else:
        messages.error(request, "We couldn't send the email just now. Please try again in a few minutes.")

    back = request.META.get('HTTP_REFERER', '')
    if url_has_allowed_host_and_scheme(back, allowed_hosts={request.get_host()},
                                       require_https=request.is_secure()):
        return redirect(back)
    return redirect('profile')


@login_required
def profile(request):
    from .models import Profile
    from generator.guards import _validate_photo_upload
    user_profile, created = Profile.objects.get_or_create(user=request.user)

    if request.method == 'POST':
        form = ProfilePreferencesForm(request.POST, instance=user_profile)
        if not form.is_valid():
            field, errors = next(iter(form.errors.items()))
            messages.error(request, f'Could not save your defaults: {errors[0]}')
            return redirect('profile')
        form.save(commit=False)

        if 'avatar' in request.FILES:
            avatar_file = request.FILES['avatar']
            ok, err = _validate_photo_upload(avatar_file)
            if not ok:
                messages.error(request, f'Avatar upload failed: {err}')
                return redirect('profile')
            user_profile.avatar = avatar_file
            
        # update_fields, not a full save(): this instance was read at the start
        # of the request, and a full save would write its stale plan and quota
        # counters back — undoing a webhook upgrade or a generation that landed
        # in between.
        user_profile.save(update_fields=[
            'base_resume', 'default_font', 'default_language', 'avatar',
        ])
        messages.success(request, 'Your global defaults have been saved.')
        return redirect('profile')

    request.user.profile = user_profile
    return render(request, 'users/profile.html')


# ──────────────────────────────────────────────────────────────────────────────
# STRIPE CHECKOUT  — dynamic plan parameter
# ──────────────────────────────────────────────────────────────────────────────

@login_required
def buy_premium(request, plan):
    """
    Create a Stripe Checkout session for the given plan ('pro' or 'elite').

    URL: /buy-premium/<str:plan>/
    The plan is stored in Stripe session metadata so the webhook can
    upgrade the correct tier without relying on the success-redirect URL.
    """
    # ── Validation ──────────────────────────────────────────────────────────
    if plan not in PLAN_PRICE_IDS:
        messages.error(request, f'Unknown plan "{plan}". Please choose Pro or Elite.')
        return redirect('pricing')

    price_id = PLAN_PRICE_IDS[plan]

    if not settings.STRIPE_SECRET_KEY:
        messages.error(request, 'Payment system is not configured yet. Please contact support.')
        return redirect('pricing')

    if not price_id:
        messages.error(
            request,
            f'The {PLAN_DISPLAY.get(plan, plan)} plan is not available yet. '
            f'Please contact support.'
        )
        return redirect('pricing')

    # ── Ensure profile exists ────────────────────────────────────────────────
    from .models import Profile
    user_profile, _ = Profile.objects.get_or_create(user=request.user)

    # ── Already on this plan ─────────────────────────────────────────────────
    if user_profile.plan == plan:
        messages.info(request, f'You are already on the {PLAN_DISPLAY[plan]} plan.')
        return redirect('pricing')

    # ── Already subscribed: change the plan, don't add a subscription ───────
    # A second Checkout here created a second subscription on a new customer:
    # the user was billed for both, and cancelling the old one downgraded
    # them to free. The portal switches the existing subscription instead,
    # with proration, and the change arrives as customer.subscription.updated.
    if user_profile.plan in ('pro', 'elite') and user_profile.stripe_customer_id:
        return redirect('stripe_customer_portal')

    # ── Create Checkout session ──────────────────────────────────────────────
    try:
        params = dict(
            payment_method_types=['card'],
            line_items=[{
                'price': price_id,
                'quantity': 1,
            }],
            mode='subscription',
            # ── metadata carries plan so the webhook can act without URL tricks ──
            metadata={
                'plan':    plan,
                'user_id': str(request.user.id),
            },
            subscription_data={'metadata': {'user_id': str(request.user.id)}},
            # Stripe fills in the placeholder, so payment_success can check it.
            success_url=request.build_absolute_uri('/payment-success/') + '?session_id={CHECKOUT_SESSION_ID}',
            cancel_url=request.build_absolute_uri('/pricing/'),
            client_reference_id=str(request.user.id),
        )
        # Reuse the customer from an earlier subscription. A fresh customer per
        # checkout orphaned the old one, and account deletion only cancels
        # subscriptions on the customer id it has.
        if user_profile.stripe_customer_id:
            params['customer'] = user_profile.stripe_customer_id
        elif request.user.email:
            params['customer_email'] = request.user.email
        checkout_session = stripe.checkout.Session.create(**params)
        return redirect(checkout_session.url, code=303)

    except stripe.error.StripeError as e:
        logger.exception('Stripe error creating checkout session for plan=%s: %s', plan, e)
        messages.error(request, f'Payment error: {e.user_message or str(e)}')
        return redirect('pricing')

    except Exception as e:
        logger.exception('Unexpected error in buy_premium for plan=%s: %s', plan, e)
        messages.error(request, 'An unexpected error occurred. Please try again.')
        return redirect('pricing')


# ──────────────────────────────────────────────────────────────────────────────
# PAYMENT SUCCESS  (redirect landing — webhook is the authoritative upgrader)
# ──────────────────────────────────────────────────────────────────────────────

@login_required
def payment_success(request):
    """
    Landing page after Stripe redirects back.
    The actual plan upgrade is done in stripe_webhook (authoritative).

    Confirms the Checkout session before congratulating anyone: this used to
    say "Payment received!" to whoever opened /payment-success/, paid or not.
    """
    session_id = request.GET.get('session_id', '')
    if not session_id:
        return redirect('pricing')
    try:
        session = stripe.checkout.Session.retrieve(session_id)
    except stripe.error.StripeError as exc:
        logger.warning('payment_success: could not retrieve session %s: %s', session_id, exc)
        messages.info(
            request,
            'We could not confirm your payment yet. Your plan will update as soon '
            'as Stripe confirms it.'
        )
        return redirect('profile')

    if (session.get('client_reference_id') != str(request.user.id)
            or session.get('status') != 'complete'):
        return redirect('pricing')

    messages.success(
        request,
        '🎉 Payment received! Your plan will be activated within a few seconds. '
        'Refresh the page if you do not see the change immediately.'
    )
    return redirect('profile')


# ──────────────────────────────────────────────────────────────────────────────
# GDPR ACCOUNT DELETION  — cancels Stripe subscription then deletes user
# ──────────────────────────────────────────────────────────────────────────────

def _billing_cancellation_failed(user, exc):
    logger.error(
        'delete_account: could not delete Stripe customer for user %s — account kept: %s',
        user.id, exc,
    )
    return JsonResponse(
        {'error': "We couldn't cancel your subscription just now, so your account "
                  "has not been deleted. Please try again in a few minutes."},
        status=503,
    )


@login_required
@require_POST
def delete_account(request):
    """
    GDPR-compliant permanent account deletion.

    Order of operations:
    1. Delete the Stripe customer, which cancels its subscriptions, so no
       further charges occur.
    2. Log the user out to invalidate the current session.
    3. Delete the Django User record.  All related data (Profile,
       Generation, JobApplication, AIResult) is removed via CASCADE.

    If step 1 fails, nothing is deleted. This used to log the Stripe error and
    delete the account anyway, which left Stripe billing a person who no longer
    had an account to cancel from. GDPR allows a month to complete an erasure;
    it does not require continuing to charge the card in the meantime.

    Returns JSON so the frontend can redirect programmatically.
    """
    from .models import Profile

    user = request.user
    profile = Profile.objects.filter(user=user).first()

    # ── Step 1: Stop billing ─────────────────────────────────────────────
    customer_id = profile.stripe_customer_id if profile else None
    if customer_id:
        try:
            # Deleting a customer immediately cancels all of its subscriptions.
            stripe.Customer.delete(customer_id)
            logger.info(
                'delete_account: deleted Stripe customer %s for user %s.',
                customer_id, user.id,
            )
        except stripe.error.InvalidRequestError as exc:
            if getattr(exc, 'code', None) != 'resource_missing':
                return _billing_cancellation_failed(user, exc)
            # Already gone on Stripe's side — nothing left to bill.
            logger.info('delete_account: Stripe customer %s already deleted.', customer_id)
        except Exception as exc:
            return _billing_cancellation_failed(user, exc)

    # ── Step 2: Invalidate session ────────────────────────────────────────
    logout(request)

    # Purge avatar media file if present (GDPR compliance)
    if profile and profile.avatar:
        try:
            profile.avatar.delete(save=False)
        except Exception:
            logger.exception('delete_account: could not delete avatar for user %s.', user.id)

    # ── Step 3: Delete user (cascades all related data) ──────────────────
    user_id = user.id
    user.delete()
    logger.info('delete_account: user %s permanently deleted.', user_id)

    return JsonResponse({'status': 'success'})


# ──────────────────────────────────────────────────────────────────────────────
# STRIPE WEBHOOK  — authoritative plan changes
# ──────────────────────────────────────────────────────────────────────────────

# Subscription statuses that mean the user is no longer paying. `past_due` is
# deliberately absent: Stripe is still retrying the card, and cutting access
# during its retry window punishes an expired card as if it were a cancellation.
# If the retries fail, Stripe moves the subscription to `unpaid` or `canceled`.
_LAPSED_STATUSES = {'unpaid', 'canceled', 'incomplete_expired', 'paused'}

_TIER_RANK = {'free': 0, 'pro': 1, 'elite': 2}


def _plan_for_price(price_id):
    """Reverse of PLAN_PRICE_IDS; None for a price this app does not sell."""
    for plan, pid in PLAN_PRICE_IDS.items():
        if pid and pid == price_id:
            return plan
    return None


def _subscription_price_id(subscription):
    # Indexed, not attribute access: on a StripeObject `.items` is dict.items.
    try:
        return subscription['items']['data'][0]['price']['id']
    except (KeyError, IndexError, TypeError):
        return None


def _profile_for_subscription(subscription):
    """
    The profile this subscription backs, or None.

    Matched by subscription id. Profiles upgraded before the id was stored
    have none, so for those — and only those — the customer id stands in.
    """
    from .models import Profile
    profile = Profile.objects.filter(stripe_subscription_id=subscription['id']).first()
    if profile is None and subscription.get('customer'):
        profile = Profile.objects.filter(
            stripe_customer_id=subscription['customer'], stripe_subscription_id='',
        ).first()
    return profile


def _downgrade_to_free(profile):
    from .models import Profile
    # .update(), not save(): save() writes every column from this request's
    # stale copy, which would also rewind the quota counters.
    Profile.objects.filter(pk=profile.pk).update(
        plan='free', is_premium=False, generations_count=3, stripe_subscription_id='',
    )


def _on_checkout_completed(session):
    from .models import Profile
    user_id = session.get('client_reference_id')
    if not user_id:
        logger.warning('Stripe webhook: checkout.session.completed missing client_reference_id.')
        return

    # Determine target plan — prefer metadata, fall back to 'pro'
    target_plan = (session.get('metadata') or {}).get('plan', 'pro')
    if target_plan not in ('pro', 'elite'):
        logger.warning(
            'Stripe webhook: unexpected plan value "%s" in metadata — defaulting to pro.',
            target_plan,
        )
        target_plan = 'pro'

    profile = Profile.objects.filter(user_id=int(user_id)).first()
    if profile is None:
        logger.error(
            'Stripe webhook: payment success but Profile not found '
            'for User ID: %s. Manual upgrade required!', user_id,
        )
        return

    # Only upgrade — never silently downgrade
    if _TIER_RANK[target_plan] < _TIER_RANK.get(profile.plan, 0):
        logger.warning(
            'Stripe webhook: skipping downgrade from "%s" to "%s" for user %s '
            '(subscription %s) — check for a duplicate subscription.',
            profile.plan, target_plan, user_id, session.get('subscription'),
        )
        return

    changes = {'plan': target_plan, 'is_premium': True}
    if session.get('customer'):
        changes['stripe_customer_id'] = session['customer']
    if session.get('subscription'):
        changes['stripe_subscription_id'] = session['subscription']
    Profile.objects.filter(pk=profile.pk).update(**changes)
    logger.info('Stripe webhook: User %s upgraded to plan "%s".', user_id, target_plan)


def _on_subscription_updated(subscription):
    """Plan switches made in the Customer Portal, and payment lapses."""
    from .models import Profile
    profile = _profile_for_subscription(subscription)
    if profile is None:
        logger.warning('Stripe webhook: subscription.updated for unknown subscription %s.',
                       subscription['id'])
        return

    status = subscription.get('status')
    if status in _LAPSED_STATUSES:
        _downgrade_to_free(profile)
        logger.info('Stripe webhook: subscription %s is %s — user %s downgraded to free.',
                    subscription['id'], status, profile.user_id)
        return

    plan = _plan_for_price(_subscription_price_id(subscription))
    if plan is None:
        logger.warning('Stripe webhook: subscription %s has an unrecognised price — plan unchanged.',
                       subscription['id'])
        return
    Profile.objects.filter(pk=profile.pk).update(
        plan=plan, is_premium=True, stripe_subscription_id=subscription['id'],
    )
    logger.info('Stripe webhook: user %s is now on "%s" (subscription %s, %s).',
                profile.user_id, plan, subscription['id'], status)


def _on_subscription_deleted(subscription):
    profile = _profile_for_subscription(subscription)
    if profile is None:
        # Includes a stale subscription on a customer whose current one is
        # different — that user is still paying and must keep their plan.
        logger.warning(
            'Stripe webhook: subscription.deleted for %s matches no active plan — ignored.',
            subscription['id'],
        )
        return
    _downgrade_to_free(profile)
    logger.info('Stripe webhook: subscription %s cancelled — user %s downgraded to free.',
                subscription['id'], profile.user_id)


_EVENT_HANDLERS = {
    'checkout.session.completed':    _on_checkout_completed,
    'customer.subscription.updated': _on_subscription_updated,
    'customer.subscription.deleted': _on_subscription_deleted,
}


@csrf_exempt
@require_POST
def stripe_webhook(request):
    """
    Stripe sends a signed POST here for each subscribed event.

    Idempotent by construction: the event is recorded in the same transaction
    as the change it makes. A duplicate delivery finds the record and stops; a
    delivery that fails part-way rolls the record back with the change and
    answers 500, so Stripe's retry applies it.
    """
    from .models import StripeEvent

    payload    = request.body
    sig_header = request.META.get('HTTP_STRIPE_SIGNATURE', '')

    if not getattr(settings, 'STRIPE_WEBHOOK_SECRET', ''):
        logger.error('STRIPE_WEBHOOK_SECRET is not set — webhook rejected.')
        return HttpResponse(status=400)

    # ── Signature verification ───────────────────────────────────────────────
    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, settings.STRIPE_WEBHOOK_SECRET
        )
    except ValueError:
        logger.warning('Stripe webhook: invalid payload.')
        return HttpResponse(status=400)
    except stripe.error.SignatureVerificationError:
        logger.warning('Stripe webhook: signature mismatch.')
        return HttpResponse(status=400)

    handler = _EVENT_HANDLERS.get(event['type'])
    if handler is None:
        logger.debug('Stripe webhook: unhandled event type "%s".', event['type'])
        return HttpResponse(status=200)

    try:
        with transaction.atomic():
            _, created = StripeEvent.objects.get_or_create(
                event_id=event['id'], defaults={'type': event['type']},
            )
            if not created:
                logger.info('Stripe webhook: duplicate event %s ignored.', event['id'])
                return HttpResponse(status=200)
            handler(event['data']['object'])
    except Exception:
        logger.exception('Stripe webhook: failed to apply %s %s — Stripe will retry.',
                         event['type'], event.get('id'))
        return HttpResponse(status=500)

    return HttpResponse(status=200)

@login_required
def stripe_customer_portal(request):
    """Redirect to the Stripe Customer Portal so users can manage/cancel subscriptions."""
    from .models import Profile

    if not getattr(settings, 'STRIPE_SECRET_KEY', None):
        logger.error('stripe_customer_portal: STRIPE_SECRET_KEY is not set.')
        messages.error(request, 'Billing is temporarily unavailable. Please try again later.')
        return redirect('pricing')

    # get_or_create: request.user.profile raised (a 500) for an account whose
    # profile had never been created.
    user_profile, _ = Profile.objects.get_or_create(user=request.user)
    if not user_profile.stripe_customer_id:
        return render(request, 'users/billing_error.html')

    try:
        session = stripe.billing_portal.Session.create(
            customer=user_profile.stripe_customer_id,
            return_url=request.build_absolute_uri(reverse('profile')),
        )
        return redirect(session.url)
    except Exception:
        # Logged, not shown: the exception text is Stripe's internals, and it
        # was being returned to the user as a bare text page.
        logger.exception('stripe_customer_portal: could not open portal for user %s', request.user.id)
        messages.error(request, "We couldn't open the billing portal. Please try again in a minute.")
        return redirect('profile')
