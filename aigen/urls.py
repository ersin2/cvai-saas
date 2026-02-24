"""
URL configuration for aigen project.
"""
from django.contrib import admin
from django.urls import path, include
from django.contrib.auth import views as auth_views
from users import views as user_views
from django.views.generic import TemplateView

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', include('generator.urls')),
    path('register/', user_views.register, name='register'),
    path('login/', auth_views.LoginView.as_view(template_name='users/login.html'), name='login'),
    path('logout/', auth_views.LogoutView.as_view(template_name='users/logout.html'), name='logout'),
    path('profile/', user_views.profile, name='profile'),
    path('pricing/', TemplateView.as_view(template_name='generator/pricing.html'), name='pricing'),
    path('buy-premium/', user_views.buy_premium, name='buy_premium'),
    path('payment-success/', user_views.payment_success, name='payment_success'),
    path('stripe-webhook/', user_views.stripe_webhook, name='stripe_webhook'),
    path('terms/', TemplateView.as_view(template_name='terms.html'), name='terms'),
    path('privacy/', TemplateView.as_view(template_name='privacy.html'), name='privacy'),
]
