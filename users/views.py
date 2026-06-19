import logging
import stripe

from django.shortcuts import render, redirect
from django.urls import reverse
from .forms import UserRegisterForm
from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.conf import settings

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

def register(request):
    if request.method == 'POST':
        form = UserRegisterForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            messages.success(
                request,
                f'Welcome, {user.username}! Your account is ready.'
            )
            return redirect('dashboard')
    else:
        form = UserRegisterForm()
    return render(request, 'users/register.html', {'form': form})


@login_required
def profile(request):
    from .models import Profile
    user_profile, created = Profile.objects.get_or_create(user=request.user)
    
    if request.method == 'POST':
        user_profile.base_resume = request.POST.get('base_resume', '')
        user_profile.default_font = request.POST.get('default_font', 'Inter')
        user_profile.default_language = request.POST.get('default_language', 'English')
        
        if 'avatar' in request.FILES:
            user_profile.avatar = request.FILES['avatar']
            
        user_profile.save()
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

    # ── Create Checkout session ──────────────────────────────────────────────
    try:
        checkout_session = stripe.checkout.Session.create(
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
            success_url=request.build_absolute_uri('/payment-success/'),
            cancel_url=request.build_absolute_uri('/pricing/'),
            client_reference_id=str(request.user.id),
            customer_email=request.user.email if request.user.email else None,
        )
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
    This view just shows a congratulation message.
    """
    messages.success(
        request,
        '🎉 Payment received! Your plan will be activated within a few seconds. '
        'Refresh the page if you do not see the change immediately.'
    )
    return redirect('profile')


# ──────────────────────────────────────────────────────────────────────────────
# GDPR ACCOUNT DELETION  — cancels Stripe subscription then deletes user
# ──────────────────────────────────────────────────────────────────────────────

@login_required
@require_POST
def delete_account(request):
    """
    GDPR-compliant permanent account deletion.

    Order of operations:
    1. Cancel / delete the Stripe customer so no further charges occur.
       Stripe errors are logged but never block the Django deletion —
       the user’s right to erasure must be fulfilled even if Stripe is down.
    2. Log the user out to invalidate the current session.
    3. Delete the Django User record.  All related data (Profile,
       Generation, JobApplication, AIResult) is removed via CASCADE.

    Returns JSON so the frontend can redirect programmatically.
    """
    user = request.user

    # ── Step 1: Cancel Stripe customer / subscriptions ───────────────────
    try:
        from .models import Profile
        profile = Profile.objects.get(user=user)
        customer_id = profile.stripe_customer_id

        if customer_id:
            # Cancel all active subscriptions before deleting the customer
            # so Stripe does not attempt a final invoice on deletion.
            subscriptions = stripe.Subscription.list(
                customer=customer_id, status='active', limit=10
            )
            for sub in subscriptions.auto_paging_iter():
                stripe.Subscription.cancel(sub.id)
                logger.info(
                    'delete_account: cancelled Stripe subscription %s for user %s.',
                    sub.id, user.id,
                )

            stripe.Customer.delete(customer_id)
            logger.info(
                'delete_account: deleted Stripe customer %s for user %s.',
                customer_id, user.id,
            )

    except Profile.DoesNotExist:
        pass  # No profile — nothing to cancel
    except stripe.error.StripeError as exc:
        # Log the Stripe failure but proceed with Django deletion.
        # GDPR right to erasure takes precedence over billing edge-cases.
        logger.error(
            'delete_account: Stripe error while cancelling customer for user %s: %s',
            user.id, exc,
        )
    except Exception as exc:
        logger.exception(
            'delete_account: unexpected error during Stripe cancellation for user %s: %s',
            user.id, exc,
        )

    # ── Step 2: Invalidate session ────────────────────────────────────────
    logout(request)

    # ── Step 3: Delete user (cascades all related data) ──────────────────
    user.delete()
    logger.info('delete_account: user %s permanently deleted.', user.id)

    return JsonResponse({'status': 'success'})


# ──────────────────────────────────────────────────────────────────────────────
# STRIPE WEBHOOK  — authoritative plan upgrade
# ──────────────────────────────────────────────────────────────────────────────

@csrf_exempt
@require_POST
def stripe_webhook(request):
    """
    Stripe sends a signed POST here after each successful checkout.

    Reads metadata['plan'] to decide which tier to assign.
    Supported plans: 'pro', 'elite'
    Falls back to 'pro' if metadata is absent (legacy compatibility).
    """
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

    # ── checkout.session.completed ───────────────────────────────────────────
    if event['type'] == 'checkout.session.completed':
        session = event['data']['object']
        user_id = session.get('client_reference_id')
        metadata = session.get('metadata', {})

        # Determine target plan — prefer metadata, fall back to 'pro'
        target_plan = metadata.get('plan', 'pro')
        if target_plan not in ('pro', 'elite'):
            logger.warning(
                'Stripe webhook: unexpected plan value "%s" in metadata — defaulting to pro.',
                target_plan
            )
            target_plan = 'pro'

        if user_id:
            from .models import Profile
            try:
                user_profile = Profile.objects.get(user_id=int(user_id))

                # Only upgrade — never silently downgrade
                tier_rank = {'free': 0, 'pro': 1, 'elite': 2}
                current_rank = tier_rank.get(user_profile.plan, 0)
                target_rank  = tier_rank.get(target_plan, 1)

                if target_rank >= current_rank:
                    user_profile.plan       = target_plan
                    user_profile.is_premium = True
                    # Persist Stripe customer ID — required for subscription.deleted lookup
                    customer_id = session.get('customer')
                    if customer_id:
                        user_profile.stripe_customer_id = customer_id
                    # Reset generation counter for upgraded free users
                    if target_plan in ('pro', 'elite'):
                        user_profile.generations_count = 9999
                    user_profile.save()
                    logger.info(
                        'Stripe webhook: User %s upgraded to plan "%s".',
                        user_id, target_plan
                    )
                else:
                    logger.info(
                        'Stripe webhook: skipping downgrade from "%s" to "%s" for user %s.',
                        user_profile.plan, target_plan, user_id
                    )

            except Profile.DoesNotExist:
                logger.error(
                    'Stripe webhook: payment success but Profile not found '
                    'for User ID: %s. Manual upgrade required!',
                    user_id
                )
        else:
            logger.warning('Stripe webhook: checkout.session.completed missing client_reference_id.')

    # ── customer.subscription.deleted (cancellation / churn) ────────────────
    elif event['type'] == 'customer.subscription.deleted':
        subscription = event['data']['object']
        customer_id  = subscription.get('customer')

        if customer_id:
            from .models import Profile
            try:
                user_profile = Profile.objects.get(stripe_customer_id=customer_id)
                user_profile.plan             = 'free'
                user_profile.is_premium       = False
                user_profile.generations_count = 3
                user_profile.save()
                logger.info(
                    'Stripe webhook: subscription cancelled for customer %s — downgraded to free.',
                    customer_id
                )
            except Profile.DoesNotExist:
                logger.warning(
                    'Stripe webhook: subscription.deleted but no Profile found '
                    'for customer %s.',
                    customer_id
                )

    else:
        logger.debug('Stripe webhook: unhandled event type "%s".', event['type'])

    return HttpResponse(status=200)

@login_required
def stripe_customer_portal(request):
    """Redirect to the Stripe Customer Portal so users can manage/cancel subscriptions."""
    if not getattr(settings, 'STRIPE_SECRET_KEY', None):
        from django.http import HttpResponse
        return HttpResponse('Payment system is not configured. No STRIPE_SECRET_KEY found.', status=400)

    user_profile = request.user.profile
    if not user_profile.stripe_customer_id:
        return render(request, 'users/billing_error.html')

    stripe.api_key = settings.STRIPE_SECRET_KEY
    try:
        session = stripe.billing_portal.Session.create(
            customer=user_profile.stripe_customer_id,
            return_url=request.build_absolute_uri(reverse('profile')),
        )
        return redirect(session.url)
    except Exception as e:
        from django.http import HttpResponse
        return HttpResponse(f'Error accessing billing portal: {str(e)}', status=400)
