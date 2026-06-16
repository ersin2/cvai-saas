"""
URL configuration for aigen project.
"""
import os
from django.contrib import admin
from django.urls import path, include
from django.contrib.auth import views as auth_views
from users import views as user_views
from generator import views as gen_views

urlpatterns = [
    path(os.getenv('ADMIN_URL_PATH', 'admin/'), admin.site.urls),
    path('', include('generator.urls')),
    path('register/', user_views.register, name='register'),
    path('login/', auth_views.LoginView.as_view(template_name='users/login.html'), name='login'),
    path('logout/', auth_views.LogoutView.as_view(template_name='users/logout.html'), name='logout'),
    path('profile/', user_views.profile, name='profile'),
    path('pricing/', gen_views.pricing, name='pricing'),
    path('buy-premium/<str:plan>/', user_views.buy_premium, name='buy_premium'),
    path('payment-success/', user_views.payment_success, name='payment_success'),
    path('stripe-webhook/', user_views.stripe_webhook, name='stripe_webhook'),
    path('terms/', gen_views.terms, name='terms'),
    path('privacy/', gen_views.privacy, name='privacy'),
    path('delete-account/', user_views.delete_account, name='delete_account'),
]

