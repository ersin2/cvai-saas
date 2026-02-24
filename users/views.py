import logging
import stripe
from django.shortcuts import render, redirect
from django.contrib.auth.forms import UserCreationForm
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.conf import settings

logger = logging.getLogger(__name__)

stripe.api_key = settings.STRIPE_SECRET_KEY



def register(request):
    if request.method == 'POST':
        form = UserCreationForm(request.POST)
        if form.is_valid():
            form.save()
            username = form.cleaned_data.get('username')
            messages.success(request, f'Account created for {username}! You can now log in.')
            return redirect('login')
    else:
        form = UserCreationForm()
    return render(request, 'users/register.html', {'form': form})


@login_required
def profile(request):
    return render(request, 'users/profile.html')


@login_required
def buy_premium(request):
    """Create a Stripe Checkout session for premium upgrade."""
    if not settings.STRIPE_SECRET_KEY or not settings.STRIPE_PRICE_ID:
        messages.error(request, 'Payment system is not configured yet.')
        return redirect('pricing')

    try:
        checkout_session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{
                'price': settings.STRIPE_PRICE_ID,
                'quantity': 1,
            }],
            mode='subscription',
            success_url=request.build_absolute_uri('/payment-success/'),
            cancel_url=request.build_absolute_uri('/pricing/'),
            client_reference_id=str(request.user.id),
            customer_email=request.user.email if request.user.email else None,
        )
        return redirect(checkout_session.url, code=303)
    except Exception as e:
        messages.error(request, f'Payment error: {str(e)}')
        return redirect('pricing')


@login_required
def payment_success(request):
    """Upgrade user after successful Stripe payment."""
    profile = request.user.profile
    if profile.plan != 'pro':
        profile.plan = 'pro'
        profile.is_premium = True
        profile.save()
    messages.success(request, '🎉 Welcome to Pro! Your account has been upgraded.')
    return redirect('profile')


@csrf_exempt
@require_POST
def stripe_webhook(request):
    """Handle Stripe webhook to confirm payment and upgrade user."""
    payload = request.body
    sig_header = request.META.get('HTTP_STRIPE_SIGNATURE', '')

    if not settings.STRIPE_WEBHOOK_SECRET:
        return HttpResponse(status=400)

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, settings.STRIPE_WEBHOOK_SECRET
        )
    except ValueError:
        return HttpResponse(status=400)
    except stripe.error.SignatureVerificationError:
        return HttpResponse(status=400)

    # Handle successful payment
    if event['type'] == 'checkout.session.completed':
        session = event['data']['object']
        user_id = session.get('client_reference_id')

        if user_id:
            from .models import Profile
            try:
                profile = Profile.objects.get(user_id=int(user_id))
                profile.plan = 'pro'
                profile.is_premium = True
                profile.save()
            except Profile.DoesNotExist:
                logger.error(
                    f"STRIPE WEBHOOK: Payment success but Profile not found "
                    f"for User ID: {user_id}. Manual upgrade required!"
                )

    return HttpResponse(status=200)